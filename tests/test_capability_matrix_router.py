"""Three-stage matrix router: stage cascade, exclude semantics, overlay
solidification shortcut (incl. the matrix-authorization guard), qwen
reasoning_content fallback, and matrix CSV path relocation.

Pure logic — scripted fake LLMs, temp stores, no device.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agents.routing.capability_matrix_router import (
    FOUNDATION_CAP,
    MATRIX_CSV,
    FoundationNotApplicable,
    NoRunnableAppForCapability,
    _llm_json,
    default_matrix_csv,
    load_matrix,
    route,
)
from agents.routing.route_overlay import RouteOverlay

# Env knobs that would perturb overlay/routing behavior if leaked from the host.
_CLEAN_ENV = {
    "RELAY_ROUTE_OVERLAY": "1",
    "RELAY_ROUTE_SOLIDIFY_HITS": "3",
    "RELAY_ROUTE_SOLIDIFY_RATE": "0.8",
    "RELAY_ROUTE_MAX_FAILS": "2",
    "RELAY_TRAJ_REDACT": "0",
}


def _cap(cap_id: str, desc: str) -> dict:
    return {
        "id": cap_id,
        "description": desc,
        "examples": [],
        "executable": True,
        "handoff_to_user_required": False,
    }


def _catalog() -> dict:
    return {"apps": [
        {"app_id": "app.nav", "app_name": "NavApp", "locale": ["zh-CN"],
         "agent_name": "NavAI", "agent_description": "导航助手",
         "capabilities": [_cap("navigate_to", "导航到指定地点")]},
        {"app_id": "app.nav2", "app_name": "NavTwo", "locale": ["zh-CN"],
         "agent_name": "Nav2AI", "agent_description": "备用导航",
         "capabilities": [_cap("navigate_to", "导航到指定地点")]},
        {"app_id": "app.chat", "app_name": "ChatApp", "locale": ["en-US"],
         "agent_name": "ChatAI", "agent_description": "general assistant",
         "capabilities": [_cap(FOUNDATION_CAP, "general Q&A")]},
    ]}


def _matrix() -> dict:
    return {
        "cap_desc": {"navigate_to": "导航", FOUNDATION_CAP: "通用助手"},
        "cap_to_apps": {
            "navigate_to": ["app.nav", "app.nav2"],
            FOUNDATION_CAP: ["app.chat"],
        },
        "app_ids": ["app.nav", "app.nav2", "app.chat"],
    }


class FakeLLM:
    """Scripted chat client: each create() pops the next payload dict and
    returns it as a ```json``` fenced reply. Records every call's kwargs."""

    def __init__(self, replies: list[dict] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.replies:
            raise AssertionError("FakeLLM called more times than scripted")
        payload = self.replies.pop(0)
        content = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
        msg = SimpleNamespace(content=content, reasoning_content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class _RawLLM:
    """Returns one fixed message object (for _llm_json edge cases)."""

    def __init__(self, content, reasoning=None) -> None:
        msg = SimpleNamespace(content=content, reasoning_content=reasoning)
        resp = SimpleNamespace(choices=[SimpleNamespace(message=msg)])
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: resp)
        )


class StageCascadeTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, _CLEAN_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stage1_then_stage2_pick(self):
        llm = FakeLLM([
            {"capability_ids": ["navigate_to"], "reason": "nav intent"},
            {"kind": "app", "app_id": "app.nav", "capability_id": "navigate_to",
             "goal": "导航到虹桥", "reason": "best fit"},
        ])
        decision = route("去虹桥火车站", _catalog(), _matrix(), llm, "m")
        self.assertEqual(decision["app_id"], "app.nav")
        self.assertEqual(decision["capability_id"], "navigate_to")
        self.assertEqual(decision["goal"], "导航到虹桥")
        self.assertEqual(len(llm.calls), 2)
        # stage-1 menu must not offer the foundation capability
        stage1_user = llm.calls[0]["messages"][1]["content"]
        self.assertNotIn(FOUNDATION_CAP, stage1_user)

    def test_stage1_filters_invalid_and_duplicate_ids(self):
        llm = FakeLLM([
            {"capability_ids": ["navigate_to", "navigate_to", "bogus", FOUNDATION_CAP]},
            {"kind": "app", "app_id": "app.nav", "capability_id": "navigate_to",
             "goal": "g", "reason": "r"},
        ])
        decision = route("去虹桥", _catalog(), _matrix(), llm, "m")
        self.assertEqual(decision["app_id"], "app.nav")
        # stage-2 shortlist was built only from the one valid capability
        stage2_user = llm.calls[1]["messages"][1]["content"]
        self.assertNotIn("bogus", stage2_user)

    def test_preserve_goal_keeps_original_nl(self):
        llm = FakeLLM([
            {"capability_ids": ["navigate_to"]},
            {"kind": "app", "app_id": "app.nav", "capability_id": "navigate_to",
             "goal": "rewritten", "reason": "r"},
        ])
        decision = route("去虹桥", _catalog(), _matrix(), llm, "m", preserve_goal=True)
        self.assertEqual(decision["goal"], "去虹桥")

    def test_stage2_single_candidate_early_exit_skips_llm(self):
        # exclude one nav app -> exactly one option left -> no stage-2 LLM call
        llm = FakeLLM([{"capability_ids": ["navigate_to"]}])
        decision = route(
            "去虹桥", _catalog(), _matrix(), llm, "m",
            exclude={("app.nav", "navigate_to")},
        )
        self.assertEqual(decision["app_id"], "app.nav2")
        self.assertEqual(len(llm.calls), 1)

    def test_exclude_all_pairs_raises_no_runnable_app(self):
        llm = FakeLLM([{"capability_ids": ["navigate_to"]}])
        with self.assertRaises(NoRunnableAppForCapability):
            route(
                "去虹桥", _catalog(), _matrix(), llm, "m",
                exclude={("app.nav", "navigate_to"), ("app.nav2", "navigate_to")},
            )

    def test_stage1_empty_falls_to_stage3(self):
        llm = FakeLLM([
            {"capability_ids": []},
            {"kind": "app", "app_id": "app.chat", "goal": "answer this", "reason": "r"},
        ])
        decision = route("什么是相对论", _catalog(), _matrix(), llm, "m")
        self.assertEqual(decision["app_id"], "app.chat")
        self.assertEqual(decision["capability_id"], FOUNDATION_CAP)

    def test_stage2_none_falls_to_stage3(self):
        llm = FakeLLM([
            {"capability_ids": ["navigate_to"]},
            {"kind": "none", "reason": "nothing fits"},
            {"kind": "app", "app_id": "app.chat", "goal": "g", "reason": "r"},
        ])
        decision = route("some request", _catalog(), _matrix(), llm, "m")
        self.assertEqual(decision["capability_id"], FOUNDATION_CAP)
        self.assertEqual(len(llm.calls), 3)

    def test_stage2_off_shortlist_pair_falls_to_stage3(self):
        llm = FakeLLM([
            {"capability_ids": ["navigate_to"]},
            {"kind": "app", "app_id": "app.hallucinated",
             "capability_id": "navigate_to", "goal": "g", "reason": "r"},
            {"kind": "app", "app_id": "app.chat", "goal": "g", "reason": "r"},
        ])
        decision = route("some request", _catalog(), _matrix(), llm, "m")
        self.assertEqual(decision["app_id"], "app.chat")

    def test_stage3_none_raises_foundation_not_applicable(self):
        llm = FakeLLM([
            {"capability_ids": []},
            {"kind": "none", "reason": "needs a device action"},
        ])
        with self.assertRaises(FoundationNotApplicable) as ctx:
            route("重命名手机里的文件", _catalog(), _matrix(), llm, "m")
        self.assertIn("device action", str(ctx.exception))

    def test_stage3_hallucinated_app_raises_runtime_error(self):
        llm = FakeLLM([
            {"capability_ids": []},
            {"kind": "app", "app_id": "app.nope", "goal": "g", "reason": "r"},
        ])
        with self.assertRaises(RuntimeError):
            route("generic request", _catalog(), _matrix(), llm, "m")

    def test_stage3_exclude_removes_foundation_app(self):
        llm = FakeLLM([{"capability_ids": []}])
        with self.assertRaises(RuntimeError) as ctx:
            route(
                "generic request", _catalog(), _matrix(), llm, "m",
                exclude={("app.chat", FOUNDATION_CAP)},
            )
        self.assertIn("cannot fall back", str(ctx.exception))


class OverlayShortcutTests(unittest.TestCase):
    """Solidification hit / miss semantics in route(), incl. the matrix guard."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, _CLEAN_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.overlay = RouteOverlay(Path(self.dir.name) / "overlay.json")
        self.key = "b:testkey"
        for _ in range(3):  # clears the default solidification bar
            self.overlay.record(self.key, "去虹桥", "app.nav", "navigate_to", "success")

    def test_hit_returns_solidified_with_zero_llm_calls(self):
        llm = FakeLLM([])  # any call would raise
        decision = route(
            "去虹桥", _catalog(), _matrix(), llm, "m",
            route_key=self.key, overlay=self.overlay,
        )
        self.assertEqual(decision["app_id"], "app.nav")
        self.assertEqual(decision["capability_id"], "navigate_to")
        self.assertEqual(len(llm.calls), 0)

    def test_hit_excluded_routes_live(self):
        llm = FakeLLM([
            {"capability_ids": ["navigate_to"]},
        ])
        decision = route(
            "去虹桥", _catalog(), _matrix(), llm, "m",
            route_key=self.key, overlay=self.overlay,
            exclude={("app.nav", "navigate_to")},
        )
        # live routing: single remaining candidate early-exit
        self.assertEqual(decision["app_id"], "app.nav2")
        self.assertEqual(len(llm.calls), 1)

    def test_hit_missing_from_catalog_routes_live(self):
        catalog = _catalog()
        catalog["apps"] = [a for a in catalog["apps"] if a["app_id"] != "app.nav"]
        llm = FakeLLM([{"capability_ids": ["navigate_to"]}])
        decision = route(
            "去虹桥", catalog, _matrix(), llm, "m",
            route_key=self.key, overlay=self.overlay,
        )
        self.assertEqual(decision["app_id"], "app.nav2")
        self.assertGreater(len(llm.calls), 0)

    def test_hit_revoked_in_matrix_routes_live(self):
        # The matrix CSV is the source of truth: revoking app.nav/navigate_to
        # must turn the solidified hit into a miss (fix for the
        # catalog-only guard), even though the manifest still declares it.
        matrix = _matrix()
        matrix["cap_to_apps"]["navigate_to"] = ["app.nav2"]
        llm = FakeLLM([{"capability_ids": ["navigate_to"]}])
        decision = route(
            "去虹桥", _catalog(), matrix, llm, "m",
            route_key=self.key, overlay=self.overlay,
        )
        self.assertEqual(decision["app_id"], "app.nav2")
        self.assertGreater(len(llm.calls), 0)

    def test_foundation_hit_revoked_in_matrix_routes_live(self):
        # Uniform guard: a solidified foundation pair is invalidated the same
        # way when the matrix drops the app's foundation_llm authorization.
        key = "b:chatkey"
        for _ in range(3):
            self.overlay.record(key, "chat", "app.chat", FOUNDATION_CAP, "success")
        matrix = _matrix()
        matrix["cap_to_apps"][FOUNDATION_CAP] = []
        llm = FakeLLM([{"capability_ids": []}])
        with self.assertRaises(RuntimeError):  # stage-3 has no app left
            route(
                "generic request", _catalog(), matrix, llm, "m",
                route_key=key, overlay=self.overlay,
            )
        self.assertEqual(len(llm.calls), 1)  # went live instead of hitting


class LlmJsonTests(unittest.TestCase):
    def test_qwen_null_content_falls_back_to_reasoning_content(self):
        fenced = '```json\n{"kind": "none"}\n```'
        llm = _RawLLM(content=None, reasoning=fenced)
        self.assertEqual(_llm_json(llm, "qwen", "sys", "user"), {"kind": "none"})

    def test_content_preferred_over_reasoning(self):
        llm = _RawLLM(content='```json\n{"a": 1}\n```', reasoning='```json\n{"a": 2}\n```')
        self.assertEqual(_llm_json(llm, "qwen", "sys", "user"), {"a": 1})

    def test_non_qwen_null_content_still_fails(self):
        # The fallback is gated on qwen (same convention as leg_judge).
        llm = _RawLLM(content=None, reasoning='```json\n{"a": 1}\n```')
        with self.assertRaises(json.JSONDecodeError):
            _llm_json(llm, "gpt-x", "sys", "user")


_MATRIX_CSV_TEXT = (
    "category,capability_id,description,Nav (app.nav),Chat (app.chat)\n"
    "nav,navigate_to,Go places,Y,\n"
    "chat,foundation_llm,General QA,,Y\n"
)


class MatrixPathRelocationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("RELAY_MATRIX_CSV", "RELAY_MANIFESTS"):
            os.environ.pop(key, None)

    def test_default_is_repo_csv(self):
        self.assertEqual(default_matrix_csv(), MATRIX_CSV)

    def test_env_override_wins(self):
        csv = self.root / "custom.csv"
        os.environ["RELAY_MATRIX_CSV"] = str(csv)
        os.environ["RELAY_MANIFESTS"] = str(self.root / "relay" / "manifests")
        self.assertEqual(default_matrix_csv(), csv)

    def test_relay_manifests_sibling_csv_used(self):
        # Android AssetInstaller layout: filesDir/relay/{manifests, csv}
        relay = self.root / "relay"
        (relay / "manifests").mkdir(parents=True)
        (relay / "app_capability_matrix.csv").write_text(_MATRIX_CSV_TEXT, encoding="utf-8")
        os.environ["RELAY_MANIFESTS"] = str(relay / "manifests")
        self.assertEqual(default_matrix_csv(), relay / "app_capability_matrix.csv")
        matrix = load_matrix()  # bare call (the leg_recovery pattern) resolves there
        self.assertEqual(matrix["cap_to_apps"]["navigate_to"], ["app.nav"])
        self.assertEqual(matrix["cap_to_apps"]["foundation_llm"], ["app.chat"])

    def test_relay_manifests_without_sibling_falls_back(self):
        (self.root / "manifests").mkdir()
        os.environ["RELAY_MANIFESTS"] = str(self.root / "manifests")
        self.assertEqual(default_matrix_csv(), MATRIX_CSV)


if __name__ == "__main__":
    unittest.main()
