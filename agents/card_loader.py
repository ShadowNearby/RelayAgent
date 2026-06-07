"""Card loading.

Single reader for the manifest schema — used by the RelayAgent adapter
and any future routing tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from loguru import logger

MANIFESTS_DIR = Path(__file__).resolve().parent.parent / "manifests"


def load_all_cards(manifests_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load every YAML card under `manifests_dir`. Errors in any single file
    are logged and the file is skipped — one bad manifest must not block
    every other card from loading."""
    d = manifests_dir or MANIFESTS_DIR
    cards = []
    for path in sorted(d.glob("*.yaml")):
        # Skip underscore-prefixed names (e.g. _draft.yaml) — they
        # are non-card content stored alongside manifests by convention.
        if path.name.startswith("_"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as e:
            logger.warning(f"Skipping unloadable manifest {path.name}: {e}")
            continue
        if not isinstance(data, dict) or "app_id" not in data:
            logger.warning(
                f"Skipping malformed manifest {path.name}: missing app_id"
            )
            continue
        cards.append(data)
    return cards


def load_card_by_app_id(app_id: str, manifests_dir: Path | None = None) -> dict[str, Any]:
    for card in load_all_cards(manifests_dir):
        if card.get("app_id") == app_id:
            return card
    raise FileNotFoundError(f"No manifest found for app_id={app_id!r} under {manifests_dir or MANIFESTS_DIR}")
