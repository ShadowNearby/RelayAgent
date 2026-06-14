"""Shared runtime configuration helpers."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("'\"")
    return out


def resolve_llm_config(
    env_file: Path,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> tuple[dict[str, str], str, str, str]:
    """Return (.env values, base_url, api_key, model), honoring overrides first."""
    env_vars = load_dotenv(env_file)
    resolved_base_url = base_url or os.getenv("LLM_BASE_URL") or env_vars.get("LLM_BASE_URL")
    resolved_api_key = api_key or os.getenv("LLM_API_KEY") or env_vars.get("LLM_API_KEY")
    resolved_model = model or os.getenv("LLM_MODEL") or env_vars.get("LLM_MODEL")
    missing = [
        name
        for name, value in [
            ("LLM_BASE_URL", resolved_base_url),
            ("LLM_API_KEY", resolved_api_key),
            ("LLM_MODEL", resolved_model),
        ]
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing required config: {', '.join(missing)}. Set in .env or env/flags."
        )
    return env_vars, resolved_base_url, resolved_api_key, resolved_model


def ensure_llm_env(
    env_file: Path,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Load .env and return a dict containing required LLM_* values."""
    env_vars = {**load_dotenv(env_file), **(overrides or {})}
    base_url = os.getenv("LLM_BASE_URL") or env_vars.get("LLM_BASE_URL")
    api_key = os.getenv("LLM_API_KEY") or env_vars.get("LLM_API_KEY")
    model = os.getenv("LLM_MODEL") or env_vars.get("LLM_MODEL")
    missing = [
        name
        for name, value in [
            ("LLM_BASE_URL", base_url),
            ("LLM_API_KEY", api_key),
            ("LLM_MODEL", model),
        ]
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing required config: {', '.join(missing)}. Set in .env or env/overrides."
        )
    env_vars["LLM_BASE_URL"] = base_url
    env_vars["LLM_API_KEY"] = api_key
    env_vars["LLM_MODEL"] = model
    return env_vars
