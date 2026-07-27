"""Pin the wait_for_reply done-detection state machine in RelayAgent
(previously device-only): the two-stage precheck (Stage-1 pixel-hash skip,
Stage-2 text-hash dump), the 3-identical-dumps done rule, the dump-failure
backoff fuse, the pixel-skip watchdog — including the fix that a
watchdog-FORCED dump never advances the stable streak (a WebView/canvas reply
keeps the a11y text hash chrome-stable while pixels stream; forced dumps
counting toward done truncated those replies) — and the
RELAY_CAPTURE_FULL_REPLY fairness gate (`capture_full and
self.capture_full_enabled`). Doneness stays VLM-free per NOTE(no-vlm-done).

Also pins wait_ms honoring the card-declared duration and the
skip-step-screenshot look-ahead (_VISION_STEP_KINDS).
"""
from __future__ import annotations

import itertools
import types
import unittest
from unittest import mock

from agents.agent.action_planner import Step
from agents.agent.relay_agent import RelayAgent

MOD = "agents.agent.relay_agent"
REPLY = "这是一段足够长的助手回复内容，超过二十五个字符阈值。"


def _agent(**over) -> RelayAgent:
    a = RelayAgent.__new__(RelayAgent)
    a.precheck_enabled = True
    a.scrape_enabled = True
    a.capture_full_enabled = True
    a.skip_step_screenshot = False
    RelayAgent.reset(a)
    a._last_input_text = "帮我查天气"
    for k, v in over.items():
        setattr(a, k, v)
    return a


class _WaitReplyBase(unittest.TestCase):
    def setUp(self):
        self.pixel = mock.Mock(name="pixel_hash", return_value="P")
        self.dump = mock.Mock(name="text_hash", return_value="T")
        self.scrape = mock.Mock(name="scrape", return_value=REPLY)
        self.swipe = mock.Mock(name="swipe_down")
        self.sleep = mock.Mock(name="settle_or_sleep")
        for target, repl in (
            (f"{MOD}._hash_screenshot_region", self.pixel),
            (f"{MOD}._dump_visible_text_hash", self.dump),
            (f"{MOD}._extract_reply_text_from_dump", self.scrape),
            (f"{MOD}.swipe_down", self.swipe),
            (f"{MOD}._settle_or_sleep", self.sleep),
        ):
            p = mock.patch(target, repl)
            p.start()
            self.addCleanup(p.stop)
        self.agent = _agent()

    def tick(self, capture_full=False, max_seconds=60):
        payload = {"max_seconds": max_seconds}
        if capture_full:
            payload["capture_full"] = True
        return self.agent._materialize(
            Step("wait_for_reply", payload), object(), 1080, 2400
        )


class PrecheckStageTests(_WaitReplyBase):
    def test_stage1_pixel_change_skips_dump(self):
        self.pixel.side_effect = ["p1", "p2", "p3"]
        for i in range(3):
            action, advance, note = self.tick()
            self.assertFalse(advance)
            self.assertIn("precheck skip", note)
        self.dump.assert_not_called()

    def test_three_stable_dumps_declare_done(self):
        # tick1: first pixel hash (None → P) counts as changed → skip.
        _, advance, note = self.tick()
        self.assertIn("precheck skip", note)
        # ticks 2-3: gated dumps, streak 1 → 2, still holding.
        for expect in ("text stable 1/3", "text stable 2/3"):
            _, advance, note = self.tick()
            self.assertFalse(advance)
            self.assertIn(expect, note)
        # tick 4: streak 3 → done, scrape wins, state reset, cursor advances.
        action, advance, note = self.tick()
        self.assertTrue(advance)
        self.assertIn("done; text=", note)
        self.assertEqual(self.agent._last_agent_reply, REPLY)
        self.assertIsNone(self.agent._reply_start_ts)
        self.assertEqual(self.agent._reply_text_stable_streak, 0)

    def test_precheck_disabled_dumps_every_tick(self):
        self.agent.precheck_enabled = False
        self.tick()
        self.tick()
        self.assertEqual(self.dump.call_count, 2)
        self.pixel.assert_not_called()


class DumpFailureFuseTests(_WaitReplyBase):
    def test_two_failures_arm_cooldown_backoff(self):
        self.dump.return_value = None
        self.tick()  # pixel warm-up skip
        _, advance, note = self.tick()
        self.assertIn("dump failed", note)
        _, advance, note = self.tick()  # 2nd failure → MAX_DUMP_FAILS → cooldown armed
        self.assertIn("dump failed", note)
        self.assertIsNotNone(self.agent._reply_dump_cooldown_until)
        _, advance, note = self.tick()
        self.assertIn("dump cooling down", note)
        self.assertEqual(self.dump.call_count, 2)  # no dump during cooldown

    def test_success_after_failure_clears_streak(self):
        self.dump.side_effect = [None, "T", "T", "T"]
        self.tick()  # pixel warm-up
        self.tick()  # dump fail (streak 1 < MAX_DUMP_FAILS)
        self.tick()  # success → streak cleared
        self.assertEqual(self.agent._reply_dump_fail_streak, 0)
        self.assertIsNone(self.agent._reply_dump_cooldown_until)


class WatchdogTests(_WaitReplyBase):
    def test_forced_dumps_fire_but_never_advance_done(self):
        # Pixels change EVERY tick (streaming WebView reply / spinner) while
        # the a11y text hash is chrome-only and constant from the first dump.
        counter = itertools.count()
        streaming = [True]
        self.pixel.side_effect = (
            lambda _shot: f"p{next(counter)}" if streaming[0] else "P-final"
        )

        # 18 ticks = 3 watchdog windows (5 skips + 1 forced dump each).
        for _ in range(18):
            _, advance, _note = self.tick()
            self.assertFalse(advance)
        self.assertEqual(self.dump.call_count, 3)  # forced at ticks 6/12/18
        # The fix under test: identical FORCED dumps must not accrue the
        # stable streak (pre-fix this would be 3 → premature done at tick 18
        # with a truncated streaming reply).
        self.assertEqual(self.agent._reply_text_stable_streak, 1)
        self.scrape.assert_not_called()

        # Screen settles → pixel-gated dumps resume and converge normally.
        streaming[0] = False
        _, advance, note = self.tick()  # p17→P-final: one last "changed" skip
        self.assertIn("precheck skip", note)
        _, advance, _ = self.tick()  # gated dump, streak 2
        self.assertFalse(advance)
        _, advance, note = self.tick()  # gated dump, streak 3 → done
        self.assertTrue(advance)
        self.assertIn("done; text=", note)
        self.assertEqual(self.agent._last_agent_reply, REPLY)

    def test_watchdog_resets_after_forced_dump(self):
        counter = itertools.count()
        self.pixel.side_effect = lambda _shot: f"p{next(counter)}"
        for _ in range(6):
            self.tick()
        self.assertEqual(self.dump.call_count, 1)
        self.assertEqual(self.agent._reply_precheck_skips_since_vlm, 0)


class TimeoutTests(_WaitReplyBase):
    def test_timeout_scrapes_and_advances(self):
        counter = itertools.count()
        self.pixel.side_effect = lambda _shot: f"p{next(counter)}"
        self.tick()  # anchors _reply_start_ts
        self.agent._reply_start_ts -= 100  # push past the 60s ceiling
        action, advance, note = self.tick()
        self.assertTrue(advance)
        self.assertEqual(note, "timeout")
        self.assertEqual(self.agent._last_agent_reply, REPLY)
        self.assertIsNone(self.agent._reply_start_ts)


class EmptyScrapeTests(_WaitReplyBase):
    def _drive_to_done_gate(self):
        self.tick()  # pixel warm-up
        self.tick()  # streak 1
        self.tick()  # streak 2
        return self.tick()  # streak 3 → done gate (scrape/VLM)

    def test_vlm_reads_text_when_scrape_empty(self):
        self.scrape.return_value = None
        self.agent._poll_agent_reply = mock.Mock(return_value="VLM 读到的文本")
        _, advance, note = self._drive_to_done_gate()
        self.assertTrue(advance)
        self.assertEqual(self.agent._last_agent_reply, "VLM 读到的文本")

    def test_holds_when_scrape_and_vlm_both_empty(self):
        self.scrape.return_value = None
        self.agent._poll_agent_reply = mock.Mock(return_value=None)
        _, advance, note = self._drive_to_done_gate()
        self.assertFalse(advance)
        self.assertIn("empty scrape+vlm", note)
        self.assertEqual(self.agent._reply_text_stable_streak, 0)  # re-arm


class CaptureFullGateTests(_WaitReplyBase):
    def _drive_to_done(self, capture_full):
        self.tick(capture_full=capture_full)
        self.tick(capture_full=capture_full)
        self.tick(capture_full=capture_full)
        return self.tick(capture_full=capture_full)

    def test_gate_off_returns_first_frame_text_no_scrolling(self):
        # RELAY_CAPTURE_FULL_REPLY=0 (A/B fairness vs the MW baseline): done
        # must return the first visible frame's text and NEVER enter the
        # scrolling capture phase, even when the card opted in.
        self.agent.capture_full_enabled = False
        _, advance, note = self._drive_to_done(capture_full=True)
        self.assertTrue(advance)
        self.assertIn("done; text=", note)
        self.assertIsNone(self.agent._capture_phase)
        self.swipe.assert_not_called()

    def test_gate_on_enters_scroll_phase_and_finishes(self):
        _, advance, note = self._drive_to_done(capture_full=True)
        self.assertFalse(advance)
        self.assertEqual(note, "done; entering full-reply capture")
        self.assertEqual(self.agent._capture_phase, "scrolling")
        self.assertEqual(self.agent._captured_chunks, [REPLY])
        self.assertEqual(self.swipe.call_count, 1)

        # Scrolling ticks: same text twice → idle 2 → capture done.
        _, advance, _ = self.tick(capture_full=True)
        self.assertFalse(advance)
        _, advance, note = self.tick(capture_full=True)
        self.assertTrue(advance)
        self.assertEqual(note, "capture done")
        self.assertEqual(self.agent._last_agent_reply, REPLY)
        self.assertIsNone(self.agent._capture_phase)

    def test_plain_step_never_captures(self):
        _, advance, note = self._drive_to_done(capture_full=False)
        self.assertTrue(advance)
        self.swipe.assert_not_called()


class WaitMsTests(unittest.TestCase):
    def test_wait_ms_sleeps_declared_duration(self):
        # The runtime's WAIT action only sleeps RELAY_WAIT_SECONDS (~0.2s);
        # the agent itself must honor the card's `wait: {ms: N}` — via
        # _settle_or_sleep so a live scrcpy stream can settle early (N is the
        # worst-case ceiling there).
        agent = RelayAgent.__new__(RelayAgent)
        with mock.patch(f"{MOD}._settle_or_sleep") as sleep:
            action, advance, note = agent._materialize(
                Step("wait_ms", {"ms": 3000}), None, 1080, 2400
            )
        sleep.assert_called_once_with(3.0)
        self.assertEqual(action.action_type, "wait")
        self.assertTrue(advance)
        self.assertEqual(note, "3000ms")

    def test_wait_ms_zero_does_not_sleep(self):
        agent = RelayAgent.__new__(RelayAgent)
        with mock.patch(f"{MOD}._settle_or_sleep") as sleep:
            action, advance, _ = agent._materialize(
                Step("wait_ms", {}), None, 1080, 2400
            )
        sleep.assert_not_called()
        self.assertTrue(advance)


class SkipScreenshotLookaheadTests(unittest.TestCase):
    def _agent_with_plan(self, plan):
        a = _agent()
        a.skip_step_screenshot = True
        a.dismiss_permissions = False
        a._permission_dismissed_count = 0
        a._task_started = True  # skip _begin_task_once side effects
        a._planned = True
        a.plan = plan
        a.cursor = 0
        return a

    def _shot(self):
        return types.SimpleNamespace(size=(1080, 2400))

    def test_skip_tag_set_when_next_step_is_deterministic(self):
        a = self._agent_with_plan([Step("wait_ms", {}), Step("wait_ms", {})])
        with mock.patch(f"{MOD}._settle_or_sleep"):
            _, action = a.predict({"screenshot": self._shot()})
        self.assertTrue((action.action_json or {}).get("skip_screenshot"))

    def test_no_skip_before_vision_step(self):
        a = self._agent_with_plan(
            [Step("wait_ms", {}), Step("wait_for_reply", {"max_seconds": 60})]
        )
        with mock.patch(f"{MOD}._settle_or_sleep"):
            _, action = a.predict({"screenshot": self._shot()})
        self.assertFalse((action.action_json or {}).get("skip_screenshot"))

    def test_skip_tag_on_plan_exhaustion(self):
        a = self._agent_with_plan([Step("wait_ms", {})])
        with mock.patch(f"{MOD}._settle_or_sleep"):
            _, action = a.predict({"screenshot": self._shot()})
        self.assertTrue((action.action_json or {}).get("skip_screenshot"))


if __name__ == "__main__":
    unittest.main()
