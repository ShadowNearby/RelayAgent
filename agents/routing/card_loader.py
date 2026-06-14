"""Card loading.

Single reader for the manifest schema — used by the RelayAgent adapter
and any future routing tools. Because every manifest consumer (catalog
build, routing, runtime execution) routes through `load_all_cards`, the
platform filter below applies everywhere at once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from agents.device import current_platform

MANIFESTS_DIR = Path(__file__).resolve().parent.parent.parent / "manifests"


def load_all_cards(manifests_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load every YAML card under `manifests_dir` that supports the current
    platform (`RELAY_PLATFORM`, default android). Errors in any single file
    are logged and the file is skipped — one bad manifest must not block
    every other card from loading."""
    d = manifests_dir or MANIFESTS_DIR
    platform = current_platform()
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
        # Platform gate. A card that doesn't list the current platform is
        # invisible to routing/planning on it. Missing/empty `platforms`
        # (schema-invalid, but be permissive at load time) means no gate.
        declared = data.get("platforms") or []
        if declared and platform not in declared:
            logger.info(
                f"Skipping manifest {path.name}: platforms={declared} "
                f"does not include current platform {platform!r}"
            )
            continue
        cards.append(data)
    return cards


def resolve_app_id(card: dict[str, Any], platform: str | None = None) -> str:
    """The app id to drive on `platform` (default: the current one):
    `app_ids[platform]` when declared, else the card's primary `app_id`."""
    platform = platform or current_platform()
    app_ids = card.get("app_ids") or {}
    return app_ids.get(platform) or card.get("app_id") or ""


def load_card_by_app_id(app_id: str, manifests_dir: Path | None = None) -> dict[str, Any]:
    for card in load_all_cards(manifests_dir):
        if card.get("app_id") == app_id:
            return card
    raise FileNotFoundError(f"No manifest found for app_id={app_id!r} under {manifests_dir or MANIFESTS_DIR}")
