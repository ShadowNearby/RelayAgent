"""Card loading.

Single reader for the manifest schema — used by the RelayAgent adapter
and any future routing tools. Because every manifest consumer (catalog
build, routing, runtime execution) routes through `load_all_cards`, the
platform filter below applies everywhere at once.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from agents.device import current_platform

MANIFESTS_DIR = Path(__file__).resolve().parent.parent.parent / "manifests"  # host checkout default


def default_manifests_dir() -> Path:
    """Manifests dir used when the caller passes none.

    `RELAY_MANIFESTS` when set (Android relocates the packaged assets to
    filesDir — REPO_ROOT-relative paths don't exist under Chaquopy's
    AssetFinder; same env the RelayAgent adapter already consumes), else the
    repo checkout default. Resolved at call time, not import time, so entry
    points that set the env after import still take effect."""
    env = os.getenv("RELAY_MANIFESTS")
    return Path(env).expanduser() if env else MANIFESTS_DIR


def load_all_cards(manifests_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load every YAML card under `manifests_dir` that supports the current
    platform (`RELAY_PLATFORM`, default android). Errors in any single file
    are logged and the file is skipped — one bad manifest must not block
    every other card from loading."""
    d = manifests_dir or default_manifests_dir()
    platform = current_platform()
    if not d.is_dir():
        # Surface the miss: a silent empty glob here becomes an empty
        # catalog / zero-candidate routing much further downstream.
        logger.warning(f"manifests dir {d} does not exist; loading zero cards")
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
    cards = load_all_cards(manifests_dir)
    for card in cards:
        if card.get("app_id") == app_id:
            return card
    # Second pass: the current platform's mapped id (app_ids.<platform>), so a
    # runner handed a platform-specific id still resolves the card. Runs AFTER
    # the exact pass so a mapped id can never shadow another card's primary id.
    for card in cards:
        if resolve_app_id(card) == app_id:
            return card
    raise FileNotFoundError(f"No manifest found for app_id={app_id!r} under {manifests_dir or default_manifests_dir()}")
