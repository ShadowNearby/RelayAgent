"""Pin the behavior of the a11y consumers migrated from raw uiautomator XML
to the normalized UINode tree (grounding tiers/scoring, reply scrape
cropping/filtering, visible-text hash, permission-popup dismiss,
serialize_tree). These existed device-only before; the UINode seam makes
them testable with fixture trees.
"""
from __future__ import annotations

import unittest
from unittest import mock

from agents.agent.a11y_agent import serialize_tree
from agents.device import UINode
from agents.device.android import AndroidBackend
from agents.agent.relay_agent import (
    _dump_visible_text_hash,
    _extract_reply_text_from_dump,
    _ground_text_via_a11y,
)

W, H = 1080, 2400


def _node(text="", desc="", rid="", cls="", pkg="", bounds=None,
          clickable=False, focusable=False, enabled=True, scrollable=False,
          long_clickable=False) -> UINode:
    return UINode(text=text, desc=desc, resource_id=rid, class_name=cls,
                  package=pkg, bounds=bounds, clickable=clickable,
                  focusable=focusable, enabled=enabled, scrollable=scrollable,
                  long_clickable=long_clickable)


def _patch_tree(nodes):
    # The scrape/grounding helpers now live in sibling modules and resolve
    # get_backend in their own namespace, so patch both modules at once.
    backend = mock.Mock()
    backend.dump_ui_tree.return_value = nodes

    class _MultiPatch:
        def __init__(self):
            self._patchers = [
                mock.patch(f"{mod}.get_backend", return_value=backend)
                for mod in ("agents.agent.relay_reply", "agents.agent.relay_grounding")
            ]

        def __enter__(self):
            for p in self._patchers:
                p.start()
            return backend

        def __exit__(self, *exc):
            for p in self._patchers:
                p.stop()
            return False

    return _MultiPatch()


class GroundingTests(unittest.TestCase):
    def test_tier1_exact_beats_substring(self):
        nodes = [
            _node(text="发送消息", bounds=(0, 100, 100, 200), clickable=True),
            _node(text="发送", bounds=(0, 300, 100, 400)),
        ]
        with _patch_tree(nodes):
            # exact "发送" (tier 1) wins even though the substring node is clickable
            self.assertEqual(_ground_text_via_a11y("发送", W, H), (50, 350))

    def test_scoring_prefers_clickable(self):
        nodes = [
            _node(text="确认", bounds=(0, 100, 100, 200)),                  # score 1
            _node(text="确认", bounds=(0, 300, 100, 400), clickable=True),  # score 5
        ]
        with _patch_tree(nodes):
            self.assertEqual(_ground_text_via_a11y("确认", W, H), (50, 350))

    def test_tier2_reverse_substring_needs_len_gt_1(self):
        nodes = [_node(text="行", bounds=(0, 0, 10, 10))]  # 1 char, inside target
        with _patch_tree(nodes):
            self.assertIsNone(_ground_text_via_a11y("执行任务", W, H))

    def test_tier3_resource_id_endswith(self):
        nodes = [_node(rid="com.x:id/send_btn", bounds=(0, 0, 100, 100))]
        with _patch_tree(nodes):
            self.assertEqual(_ground_text_via_a11y("send_btn", W, H), (50, 50))

    def test_offscreen_and_zero_area_filtered(self):
        nodes = [
            _node(text="hi", bounds=(0, 0, 0, 0)),            # zero-area
            _node(text="hi", bounds=(2000, 0, 4000, 100)),    # center off-screen
        ]
        with _patch_tree(nodes):
            self.assertIsNone(_ground_text_via_a11y("hi", W, H))

    def test_dump_failure_returns_none(self):
        with _patch_tree(None):
            self.assertIsNone(_ground_text_via_a11y("x", W, H))


class ExtractReplyTests(unittest.TestCase):
    def test_crop_cut_and_chrome_filter(self):
        long_reply = "这是一段足够长的助手回复，超过二十五个字符的最小阈值要求了吧。"
        nodes = [
            _node(text="10:23", bounds=(0, 50, 100, 80)),          # status bar (top 8%)
            _node(text="帮我查天气", bounds=(0, 500, 500, 560)),     # user bubble
            _node(text="复制", bounds=(0, 900, 80, 950)),           # chrome
            _node(text=long_reply, bounds=(0, 1000, 1000, 1300)),  # the reply
            _node(text="发消息", bounds=(0, 2300, 500, 2380)),       # input bar (bottom 18%)
            _node(text="上文残留不应该出现这一条哦哦哦哦哦哦哦哦哦哦哦哦",
                  bounds=(0, 300, 1000, 400)),                     # above user bubble
        ]
        with _patch_tree(nodes):
            text = _extract_reply_text_from_dump("帮我查天气", H)
        self.assertEqual(text, long_reply)

    def test_short_chips_dropped_only_with_substantial_node(self):
        chip = _node(text="还有什么推荐？", bounds=(0, 1000, 400, 1060))
        with _patch_tree([chip]):
            # alone, a short line IS the reply
            self.assertEqual(_extract_reply_text_from_dump(None, H), "还有什么推荐？")
        long_reply = "这是一段足够长的助手回复，超过二十五个字符的最小阈值要求了吧。"
        with _patch_tree([chip, _node(text=long_reply, bounds=(0, 1100, 1000, 1400))]):
            self.assertEqual(_extract_reply_text_from_dump(None, H), long_reply)

    def test_dump_failure_returns_none(self):
        with _patch_tree(None):
            self.assertIsNone(_extract_reply_text_from_dump(None, H))


class VisibleTextHashTests(unittest.TestCase):
    def test_stable_and_desc_dedup(self):
        nodes_a = [_node(text="hello", desc="hello"), _node(desc="world")]
        nodes_b = [_node(text="hello"), _node(desc="world")]
        with _patch_tree(nodes_a):
            ha = _dump_visible_text_hash()
        with _patch_tree(nodes_b):
            hb = _dump_visible_text_hash()
        self.assertEqual(ha, hb)  # desc == text contributes once
        with _patch_tree([_node(text="hello"), _node(desc="world!")]):
            self.assertNotEqual(_dump_visible_text_hash(), ha)
        with _patch_tree(None):
            self.assertIsNone(_dump_visible_text_hash())


class SerializeTreeTests(unittest.TestCase):
    def test_keep_drop_and_listing(self):
        nodes = [
            _node(cls="android.widget.FrameLayout", bounds=(0, 0, W, H)),  # layout: drop
            _node(text="搜索", cls="android.widget.Button",
                  bounds=(0, 100, 200, 200), clickable=True),
            _node(cls="android.widget.EditText", desc="输入框",
                  bounds=(0, 300, 800, 400), focusable=True),
        ]
        out, listing = serialize_tree(nodes, W, H, max_nodes=60, trunc=50)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["label"], "搜索")
        self.assertTrue(out[1]["editable"])
        self.assertIn('[0] Button "搜索"', listing)
        self.assertIn("{editable}", listing)

    def test_truncation_marker(self):
        nodes = [_node(text=f"n{i}", bounds=(0, i * 10, 50, i * 10 + 9))
                 for i in range(5)]
        out, listing = serialize_tree(nodes, W, H, max_nodes=3, trunc=50)
        self.assertEqual(len(out), 3)
        self.assertIn("(list truncated)", listing)


class CropCutoffTests(unittest.TestCase):
    def test_defaults(self):
        import os
        from agents.agent.relay_agent import _crop_cutoffs
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RELAY_CROP_TOP", None)
            os.environ.pop("RELAY_CROP_BOTTOM", None)
            self.assertEqual(_crop_cutoffs(2400), (192, 1968))  # 8% / 82%

    def test_env_override_and_clamp(self):
        import os
        from agents.agent.relay_agent import _crop_cutoffs
        with mock.patch.dict(os.environ,
                             {"RELAY_CROP_TOP": "0.10", "RELAY_CROP_BOTTOM": "0.9"}):
            top, bot = _crop_cutoffs(1000)
        self.assertEqual(top, 100)
        self.assertEqual(bot, 550)  # 0.9 clamps to 0.45 → cutoff 1 - 0.45


class PermissionDismissTests(unittest.TestCase):
    def _backend(self, foreground, nodes):
        b = AndroidBackend()
        b.foreground_app = mock.Mock(return_value=foreground)  # type: ignore[method-assign]
        b.dump_ui_tree = mock.Mock(return_value=nodes)  # type: ignore[method-assign]
        b.tap = mock.Mock(return_value=True)  # type: ignore[method-assign]
        return b

    def test_fast_exit_when_foreground_not_permission_controller(self):
        b = self._backend("com.aliyun.tongyi", None)
        self.assertIsNone(b.dismiss_permission_popup())
        b.dump_ui_tree.assert_not_called()  # cheap probe short-circuits

    def test_taps_most_permissive_label_within_permission_package(self):
        pkg = "com.android.permissioncontroller"
        nodes = [
            _node(text="允许", pkg="com.aliyun.tongyi",  # in-app decoy: wrong package
                  bounds=(0, 0, 100, 100), clickable=True),
            _node(text="允许", pkg=pkg, bounds=(0, 200, 100, 300), clickable=True),
            _node(text="始终允许", pkg=pkg, bounds=(0, 400, 100, 500), clickable=True),
        ]
        b = self._backend(pkg, nodes)
        self.assertEqual(b.dismiss_permission_popup(), "始终允许")
        b.tap.assert_called_once_with(50, 450, timeout=3)

    def test_non_clickable_label_skipped(self):
        pkg = "com.android.permissioncontroller"
        nodes = [_node(text="允许", pkg=pkg, bounds=(0, 0, 100, 100))]
        b = self._backend(pkg, nodes)
        self.assertIsNone(b.dismiss_permission_popup())


if __name__ == "__main__":
    unittest.main()
