"""Report high-confidence solidified routes for human promotion review.

P3, read-only by design. The route overlay (`traj_logs/route_overlay.json`) is a
*learned* artifact; `docs/app_capability_matrix.csv` is the hand-maintained
**source of truth**. This tool never writes the matrix — it surfaces which routes
the trace has confidently learned so a human can decide whether to fold them in:

  - routes whose preferred (app, capability) is **already authorized** in the
    matrix (confirmation — the learned preference agrees with the matrix), and
  - routes pointing at an (app, capability) **not in the matrix** (a candidate
    to add a ✓, or a stale overlay entry to ignore).

Pure logic — no device, no LLM, no network. Run:

    uv run python scripts/promote_routes.py                 # report
    uv run python scripts/promote_routes.py --csv           # + review rows as CSV
    uv run python scripts/promote_routes.py --min-hits 3    # loosen the bar

The promotion bar is intentionally *higher* than the live solidification bar
(MIN_HITS/RATE) so only well-established routes are suggested.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.capability_matrix_router import load_matrix  # noqa: E402
from agents.route_overlay import RouteOverlay  # noqa: E402


def _collect(store: dict, min_hits: int, min_rate: float) -> list[dict]:
    """Flatten the overlay into per-route records that clear the promotion bar."""
    out: list[dict] = []
    for key, entry in (store or {}).items():
        if not isinstance(entry, dict):
            continue
        intent = entry.get("intent", "")
        for pair, stats in (entry.get("routes") or {}).items():
            if not isinstance(stats, dict) or "/" not in pair:
                continue
            succ = int(stats.get("success", 0))
            fail = int(stats.get("failure", 0))
            total = succ + fail
            rate = (succ / total) if total else 0.0
            if succ < min_hits or rate < min_rate:
                continue
            app, cap = pair.split("/", 1)
            out.append({
                "key": key, "intent": intent, "app": app, "cap": cap,
                "success": succ, "failure": fail, "rate": rate,
            })
    out.sort(key=lambda r: r["success"], reverse=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Report promotable solidified routes (read-only).")
    ap.add_argument("--path", default=None, help="overlay JSON (default: RELAY_ROUTE_OVERLAY_PATH or traj_logs/route_overlay.json)")
    ap.add_argument("--min-hits", type=int, default=int(os.getenv("RELAY_PROMOTE_MIN_HITS", "5")))
    ap.add_argument("--min-rate", type=float, default=float(os.getenv("RELAY_PROMOTE_MIN_RATE", "0.9")))
    ap.add_argument("--csv", action="store_true", help="also print review rows as CSV (for hand-pasting; NOT the matrix)")
    args = ap.parse_args(argv)

    path = Path(args.path) if args.path else RouteOverlay().path
    if not path.exists():
        print(f"No overlay store at {path} — nothing learned yet.")
        return 0
    try:
        store = json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"Could not read overlay at {path}: {e}")
        return 1

    matrix = load_matrix()
    cap_to_apps = matrix["cap_to_apps"]

    records = _collect(store, args.min_hits, args.min_rate)
    print(f"Route overlay: {path}")
    print(f"Promotion bar: success ≥ {args.min_hits}, rate ≥ {args.min_rate:.2f}\n")
    if not records:
        print("No routes clear the promotion bar yet.")
        return 0

    print(f"HIGH-CONFIDENCE ROUTES ({len(records)}):")
    candidates: list[dict] = []
    for r in records:
        authorized = r["app"] in cap_to_apps.get(r["cap"], [])
        tag = "matrix: authorized" if authorized else "matrix: NOT LISTED — candidate to add"
        if not authorized:
            candidates.append(r)
        intent = (r["intent"][:48] + "…") if len(r["intent"]) > 49 else r["intent"]
        print(f"  {r['cap']} → {r['app']}  "
              f"(success={r['success']} failure={r['failure']} rate={r['rate']:.2f})  "
              f"[{tag}]")
        if intent:
            print(f"      intent: {intent}")

    print(f"\n{len(candidates)} route(s) not yet in the matrix.")
    print("This tool is read-only — review and hand-edit docs/app_capability_matrix.csv if appropriate.")

    if args.csv:
        print("\n# review rows (capability_id,preferred_app,success,failure,rate,in_matrix)")
        for r in records:
            in_m = r["app"] in cap_to_apps.get(r["cap"], [])
            print(f"{r['cap']},{r['app']},{r['success']},{r['failure']},{r['rate']:.2f},{int(in_m)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
