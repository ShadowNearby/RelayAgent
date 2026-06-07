"""Shared app-card catalog helpers.

The catalog is a compact JSON-able digest of every manifest's embedded-agent
surface. It is used by both single-app routing and cross-app planning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = REPO_ROOT / "manifests"


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def build_catalog(manifest_dir: Path = MANIFEST_DIR) -> dict[str, Any]:
    """Compact JSON-able view of available apps for router/planner LLMs."""
    apps: list[dict[str, Any]] = []
    for path in sorted(manifest_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            logger.warning(f"skip {path.name}: {e}")
            continue

        agent = doc.get("embedded_agent") or {}
        caps = []
        for c in agent.get("capabilities") or []:
            caps.append({
                "id": c.get("id"),
                "description": clean_text(c.get("description")),
                "examples": c.get("example_prompts") or [],
                "executable": c.get("executable", True),
                "handoff_to_user_required": c.get("handoff_to_user_required", False),
                "x_skip_wait_for_reply": c.get("x_skip_wait_for_reply", False),
            })
        apps.append({
            "app_id": doc.get("app_id"),
            "app_name": doc.get("app_name"),
            "locale": doc.get("locale") or [],
            "agent_name": agent.get("name"),
            "agent_description": clean_text(agent.get("description")),
            "capabilities": caps,
        })

    return {"apps": apps}
