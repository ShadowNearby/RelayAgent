#!/usr/bin/env python3
"""Sync repo manifests → HarmonyOS rawfile cards (the ArkTS app's data assets).

Counterpart of android/app/build.gradle.kts `syncRelayAssets`. ArkTS has no
YAML parser, so this host-side step converts each `manifests/<app_id>.yaml`
into a slim JSON card matching `harmony/.../relay/agent.ets:Card`
({app_id, app_name, agent_name, capabilities:[{id, description, input_hint}]})
and copies the capability matrix CSV. Run before `hvigorw assembleHap`.

Usage:
    uv run python scripts/sync_harmony_assets.py            # write rawfile cards
    uv run python scripts/sync_harmony_assets.py --check    # count only, no write
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
MANIFESTS = REPO / "manifests"
MATRIX = REPO / "docs" / "app_capability_matrix.csv"
OUT_DIR = REPO / "harmony" / "entry" / "src" / "main" / "resources" / "rawfile" / "relay"


def _input_hint(agent: dict) -> str:
    """The text/desc of the message box to focus (embedded_agent.invocation.
    input.field.text), used by the ArkTS tap_text step."""
    field = (((agent.get("invocation") or {}).get("input") or {}).get("field") or {})
    return str(field.get("text") or field.get("desc") or "")


def _slim_card(doc: dict) -> dict:
    agent = doc.get("embedded_agent") or {}
    hint = _input_hint(agent)
    caps = []
    for c in agent.get("capabilities") or []:
        caps.append({
            "id": c.get("id"),
            "description": (c.get("description") or "").strip(),
            "input_hint": hint,
        })
    return {
        "app_id": doc.get("app_id"),
        "app_name": doc.get("app_name"),
        "agent_name": agent.get("name"),
        "capabilities": caps,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="count cards without writing")
    ap.add_argument("--out", type=Path, default=OUT_DIR,
                    help="rawfile/relay output dir")
    args = ap.parse_args()

    yamls = sorted(p for p in MANIFESTS.glob("*.yaml") if not p.name.startswith("_"))
    cards = []
    for path in yamls:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        card = _slim_card(doc)
        if not card["app_id"]:
            print(f"  ! skip {path.name}: no app_id", file=sys.stderr)
            continue
        cards.append(card)

    print(f"manifests: {len(yamls)} yaml → {len(cards)} card(s)")
    if args.check:
        return 0 if len(cards) == len(yamls) else 1

    man_dir = args.out / "manifests"
    man_dir.mkdir(parents=True, exist_ok=True)
    for card in cards:
        (man_dir / f"{card['app_id']}.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    if MATRIX.exists():
        (args.out / "app_capability_matrix.csv").write_text(
            MATRIX.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {len(cards)} card(s) + matrix → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
