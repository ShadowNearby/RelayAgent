"""Self-contained smoke check for the route-solidification overlay.

Pure logic — no device, no LLM, no network. Exercises the solidification
thresholds, the consecutive-failure pause, the stale-pair guard in the router,
the disable flag, and corrupt-store tolerance. Run:

    uv run python scripts/routes/check_route_overlay.py

Exits 0 on PASS, non-zero on the first failed assertion.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Pin the overlay to a throwaway file and a known threshold config BEFORE
# importing the module (config is read from env at call time, but isolating the
# path keeps this hermetic).
os.environ["RELAY_ROUTE_OVERLAY_PATH"] = tempfile.mktemp(suffix=".json")
os.environ.setdefault("RELAY_ROUTE_SOLIDIFY_HITS", "3")
os.environ.setdefault("RELAY_ROUTE_SOLIDIFY_RATE", "0.8")
os.environ.setdefault("RELAY_ROUTE_MAX_FAILS", "2")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.routing.capability_matrix_router as R  # noqa: E402
from agents.routing.route_overlay import (  # noqa: E402
    RouteOverlay,
    compute_route_key,
    route_key,
    route_key_b,
)

APP, CAP = "com.autonavi.minimap", "live_navigation"
CATALOG = {"apps": [{
    "app_id": APP, "app_name": "高德", "locale": ["zh-CN"],
    "capabilities": [{"id": CAP, "description": "导航", "examples": []}],
}]}
MATRIX = {
    "cap_desc": {CAP: "导航"},
    "cap_to_apps": {CAP: [APP]},
    "app_ids": [APP],
}


class _BoomLLM:
    """Any chat completion must raise — proves a route was served without LLM."""
    class chat:
        class completions:
            @staticmethod
            def create(*a, **k):
                raise AssertionError("LLM called on a path that should be solidified")


def _fresh() -> RouteOverlay:
    p = Path(os.environ["RELAY_ROUTE_OVERLAY_PATH"])
    if p.exists():
        p.unlink()
    return RouteOverlay()


def main() -> int:
    # key normalization: case / surrounding whitespace collapse to one key
    k = route_key("导航去机场")
    assert route_key("  导航去机场 ") == k, "key not normalized"

    ov = _fresh()
    assert ov.lookup(k) is None, "cold key should not be solidified"

    ov.record(k, "导航去机场", APP, CAP, "success")
    ov.record(k, "导航去机场", APP, CAP, "success")
    assert ov.lookup(k) is None, "2 successes is below MIN_HITS=3"

    ov.record(k, "导航去机场", APP, CAP, "success")
    assert ov.lookup(k) == (APP, CAP), "3 clean successes should solidify"

    # router short-circuits a solidified route with zero LLM calls
    d = R.route("导航去机场", CATALOG, MATRIX, _BoomLLM(), "qwen",
                preserve_goal=True, route_key=k, overlay=ov)
    assert d["app_id"] == APP and d["capability_id"] == CAP, "wrong solidified route"

    # stale pair (capability gone from catalog) must NOT short-circuit
    stale = {"apps": [{"app_id": APP, "app_name": "高德", "locale": ["zh-CN"],
                       "capabilities": []}]}
    try:
        R.route("导航去机场", stale, MATRIX, _BoomLLM(), "qwen",
                preserve_goal=True, route_key=k, overlay=ov)
        raise AssertionError("stale pair was served instead of routing live")
    except AssertionError as e:
        if "routing live" not in str(e) and "LLM called" not in str(e):
            raise  # a real assertion failure, re-raise

    # consecutive failures pause solidification (MAX_FAILS=2)
    ov2 = _fresh()
    for _ in range(5):
        ov2.record(k, "导航去机场", APP, CAP, "success")  # rate headroom
    assert ov2.lookup(k) == (APP, CAP)
    ov2.record(k, "导航去机场", APP, CAP, "failure")
    ov2.record(k, "导航去机场", APP, CAP, "failure")
    assert ov2.lookup(k) is None, "2 consecutive failures should pause the route"
    ov2.record(k, "导航去机场", APP, CAP, "success")  # resets consec_fail; rate 6/8=0.75<0.8
    # still paused by the rate gate — that's intended (chronic failures stay out)
    assert ov2.lookup(k) is None, "low success-rate route should stay paused"

    # disable flag is honored
    os.environ["RELAY_ROUTE_OVERLAY"] = "0"
    assert RouteOverlay().lookup(k) is None, "disabled overlay must never solidify"
    os.environ["RELAY_ROUTE_OVERLAY"] = "1"

    # corrupt store degrades to empty, never raises
    Path(os.environ["RELAY_ROUTE_OVERLAY_PATH"]).write_text("{not json", encoding="utf-8")
    assert RouteOverlay().lookup(k) is None, "corrupt store should read as empty"

    _check_value_independent_key()

    print("PASS: route overlay solidification checks")
    return 0


def _check_value_independent_key() -> None:
    """P3 option B: value-independent key (provisional cap | app | locale)."""
    # Same provisional triple + same request locale → SAME key for distinct
    # prompts (the cross-intent reuse this whole mode exists for).
    ka = compute_route_key("导航去人民广场", provisional_cap=CAP, provisional_app=APP, mode="b")
    kb = compute_route_key("导航去虹桥机场", provisional_cap=CAP, provisional_app=APP, mode="b")
    assert ka == kb, "same (cap,app,locale) must share one B key across intents"
    assert ka.startswith("b:"), "B keys must carry the b: prefix"

    # Request locale discriminates: Chinese nav (→ 高德) vs English nav (→ Gemini).
    ken = compute_route_key("navigate to JFK", provisional_cap=CAP, provisional_app=APP, mode="b")
    assert ken != ka, "cjk vs latin request must not collapse onto one key"

    # Locale bucket is a MAJORITY vote, not "any CJK char": a lone CJK proper
    # noun in an English request stays latin (won't match a zh-nav route).
    ken_pn = compute_route_key("navigate to 北京", provisional_cap=CAP, provisional_app=APP, mode="b")
    assert ken_pn == ken, "a lone CJK proper noun must keep the request in the latin bucket"

    # App hint discriminates.
    kapp = compute_route_key("导航去人民广场", provisional_cap=CAP, provisional_app="other.app", mode="b")
    assert kapp != ka, "different provisional app must yield a different key"

    # No provisional capability → safe fallback to option A (value-bearing).
    fb = compute_route_key("导航去人民广场", provisional_cap="", provisional_app=APP, mode="b")
    assert fb == route_key("导航去人民广场"), "missing provisional cap must fall back to A"
    assert not fb.startswith("b:"), "fallback key must be an A key"

    # Mode "a" is always value-bearing regardless of provisional info.
    ma = compute_route_key("导航去人民广场", provisional_cap=CAP, provisional_app=APP, mode="a")
    assert ma == route_key("导航去人民广场"), "mode a must ignore provisional info"

    # End-to-end cross-intent reuse: solidify under one intent's B key, a DIFFERENT
    # intent with the same provisional triple then looks up a hit (0 LLM).
    ov = _fresh()
    for _ in range(3):
        ov.record(ka, "导航去人民广场", APP, CAP, "success")
    k2 = compute_route_key("导航去南京路", provisional_cap=CAP, provisional_app=APP, mode="b")
    assert k2 == ka, "third nav intent must map to the already-solidified B key"
    assert ov.lookup(k2) == (APP, CAP), "cross-intent B-key lookup should hit, 0 LLM"

    # Direct route_key_b contract: None when no provisional capability.
    assert route_key_b("", APP, "cjk") is None, "no cap → None (caller falls back to A)"


if __name__ == "__main__":
    sys.exit(main())
