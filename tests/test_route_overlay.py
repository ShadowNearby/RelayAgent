"""Route solidification overlay store: solidify/pause thresholds, key modes,
default path relocation (RELAY_TRAJ_ROOT), record-time profile redaction, and
the read-only promotion reporter.

Pure logic — temp stores, no device, no LLM.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agents.routing.route_overlay import (
    REPO_ROOT,
    RouteOverlay,
    _default_path,
    compute_route_key,
    route_key,
    route_key_b,
)

_DEFAULT_KNOBS = {
    "RELAY_ROUTE_OVERLAY": "1",
    "RELAY_ROUTE_SOLIDIFY_HITS": "3",
    "RELAY_ROUTE_SOLIDIFY_RATE": "0.8",
    "RELAY_ROUTE_MAX_FAILS": "2",
    "RELAY_TRAJ_REDACT": "0",
}


class _OverlayBase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, _DEFAULT_KNOBS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "overlay.json"
        self.overlay = RouteOverlay(self.path)

    def _record_n(self, n: int, status: str, key="k", app="app.a", cap="cap.x"):
        for _ in range(n):
            self.overlay.record(key, "intent", app, cap, status)


class SolidifyThresholdTests(_OverlayBase):
    def test_below_min_hits_returns_none(self):
        self._record_n(2, "success")
        self.assertIsNone(self.overlay.lookup("k"))

    def test_solidifies_at_min_hits(self):
        self._record_n(3, "success")
        self.assertEqual(self.overlay.lookup("k"), ("app.a", "cap.x"))

    def test_consecutive_failures_pause_route(self):
        self._record_n(8, "success")
        self._record_n(2, "failure")  # hits RELAY_ROUTE_MAX_FAILS
        self.assertIsNone(self.overlay.lookup("k"))

    def test_success_resets_consecutive_failures(self):
        self._record_n(8, "success")
        self._record_n(2, "failure")
        self._record_n(1, "success")  # 9s/2f: rate 0.818 >= 0.8, consec reset
        self.assertEqual(self.overlay.lookup("k"), ("app.a", "cap.x"))

    def test_success_rate_gate(self):
        # interleave so consec_fail never reaches the pause bar; rate 0.5 < 0.8
        for status in ("failure", "success") * 3:
            self.overlay.record("k", "intent", "app.a", "cap.x", status)
        self.assertIsNone(self.overlay.lookup("k"))

    def test_best_pair_by_success_count_wins(self):
        self._record_n(3, "success", app="app.a")
        self._record_n(5, "success", app="app.b")
        self.assertEqual(self.overlay.lookup("k"), ("app.b", "cap.x"))

    def test_neutral_statuses_do_not_solidify(self):
        self._record_n(5, "loading")
        self._record_n(5, "unknown")
        self.assertIsNone(self.overlay.lookup("k"))

    def test_disabled_env_turns_store_off(self):
        self._record_n(3, "success")
        with mock.patch.dict(os.environ, {"RELAY_ROUTE_OVERLAY": "0"}):
            off = RouteOverlay(self.path)
            self.assertIsNone(off.lookup("k"))
            off.record("k2", "intent", "app.a", "cap.x", "success")
        self.assertNotIn("k2", json.loads(self.path.read_text(encoding="utf-8")))

    def test_corrupt_store_degrades_to_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.overlay.lookup("k"))  # no raise
        self._record_n(3, "success")  # record rebuilds a valid store
        self.assertEqual(self.overlay.lookup("k"), ("app.a", "cap.x"))


class RouteKeyTests(unittest.TestCase):
    def test_option_a_normalizes_case_and_whitespace(self):
        self.assertEqual(route_key("Navigate  to A"), route_key("navigate to a"))

    def test_option_b_requires_provisional_cap(self):
        self.assertIsNone(route_key_b(None, "app.a", "cjk"))
        self.assertIsNone(route_key_b("  ", "app.a", "cjk"))
        kb = route_key_b("navigate_to", "app.a", "cjk")
        self.assertTrue(kb.startswith("b:"))

    def test_compute_key_mode_b_falls_back_to_a(self):
        with mock.patch.dict(os.environ, {"RELAY_ROUTE_KEY_MODE": "b"}):
            self.assertEqual(compute_route_key("go home"), route_key("go home"))
            self.assertTrue(
                compute_route_key("go home", provisional_cap="navigate_to").startswith("b:")
            )

    def test_locale_bucket_splits_keys(self):
        with mock.patch.dict(os.environ, {"RELAY_ROUTE_KEY_MODE": "b"}):
            zh = compute_route_key("导航到北京西站", provisional_cap="navigate_to")
            en = compute_route_key("navigate to the station", provisional_cap="navigate_to")
            self.assertNotEqual(zh, en)


class DefaultPathTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("RELAY_ROUTE_OVERLAY_PATH", "RELAY_TRAJ_ROOT"):
            os.environ.pop(key, None)

    def test_host_default_unchanged(self):
        self.assertEqual(_default_path(), REPO_ROOT / "traj_logs" / "route_overlay.json")

    def test_traj_root_relocates_store(self):
        os.environ["RELAY_TRAJ_ROOT"] = "/data/files/traj_logs"
        self.assertEqual(
            _default_path(), Path("/data/files/traj_logs") / "route_overlay.json"
        )

    def test_explicit_path_env_wins_over_traj_root(self):
        os.environ["RELAY_TRAJ_ROOT"] = "/data/files/traj_logs"
        os.environ["RELAY_ROUTE_OVERLAY_PATH"] = "/elsewhere/ov.json"
        self.assertEqual(_default_path(), Path("/elsewhere/ov.json"))


class RecordRedactionTests(unittest.TestCase):
    """RELAY_TRAJ_REDACT=1 must scrub profile values from the persisted intent
    label — the overlay store is a traj_logs disk sink like any other."""

    ADDRESS = "幸福路1号"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        (root / "profile").mkdir()
        (root / "profile" / "profile.yaml").write_text(
            f"addresses:\n  home: {self.ADDRESS}\n", encoding="utf-8"
        )
        patcher = mock.patch.dict(os.environ, {
            "RELAY_PROFILE": "1",
            "RELAY_PROFILE_ROOT": str(root / "profile"),
            "RELAY_ROUTE_OVERLAY": "1",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = root / "overlay.json"

    def _stored_intent(self) -> str:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data["k"]["intent"]

    def test_redacts_profile_values_when_enabled(self):
        with mock.patch.dict(os.environ, {"RELAY_TRAJ_REDACT": "1"}):
            RouteOverlay(self.path).record(
                "k", f"导航到{self.ADDRESS}", "app.a", "navigate_to", "success"
            )
        self.assertEqual(self._stored_intent(), "导航到<profile:addresses.home>")

    def test_intent_kept_verbatim_when_redaction_off(self):
        with mock.patch.dict(os.environ, {"RELAY_TRAJ_REDACT": "0"}):
            RouteOverlay(self.path).record(
                "k", f"导航到{self.ADDRESS}", "app.a", "navigate_to", "success"
            )
        self.assertEqual(self._stored_intent(), f"导航到{self.ADDRESS}")


def _load_promote_routes():
    path = Path(__file__).resolve().parent.parent / "scripts" / "routes" / "promote_routes.py"
    spec = importlib.util.spec_from_file_location("promote_routes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PromoteRoutesTests(unittest.TestCase):
    """P3 promotion reporter: applies its (stricter) bar and stays read-only."""

    STORE = {
        "k1": {"intent": "去虹桥", "routes": {
            "app.a/navigate_to": {"success": 6, "failure": 0},
            "app.b/navigate_to": {"success": 2, "failure": 0},  # below bar
        }},
        "k2": {"intent": "order", "routes": {
            "app.c/order_food": {"success": 9, "failure": 0},
            "app.d/order_food": {"success": 6, "failure": 4},  # rate 0.6 < 0.9
        }},
    }

    def test_collect_applies_bar_and_sorts(self):
        mod = _load_promote_routes()
        records = mod._collect(self.STORE, min_hits=5, min_rate=0.9)
        self.assertEqual(
            [(r["app"], r["cap"], r["success"]) for r in records],
            [("app.c", "order_food", 9), ("app.a", "navigate_to", 6)],
        )

    def test_main_is_read_only(self):
        mod = _load_promote_routes()
        with tempfile.TemporaryDirectory() as d:
            store_path = Path(d) / "route_overlay.json"
            store_path.write_text(json.dumps(self.STORE), encoding="utf-8")
            before = store_path.read_bytes()
            with mock.patch.dict(os.environ, {}, clear=False):
                for key in ("RELAY_MATRIX_CSV", "RELAY_MANIFESTS"):
                    os.environ.pop(key, None)
                with redirect_stdout(io.StringIO()) as buf:
                    rc = mod.main(["--path", str(store_path)])
            self.assertEqual(rc, 0)
            self.assertEqual(store_path.read_bytes(), before)  # never rewritten
            self.assertIn("read-only", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
