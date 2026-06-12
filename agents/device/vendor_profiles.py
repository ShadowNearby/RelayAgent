"""Vendor-specific Android tables: permission-controller packages and the
Allow-button labels used by the auto-dismiss hook.

Per CLAUDE.md, every fresh capability that needs a runtime permission
(camera, location, mic, contacts, ...) gets blocked by a system dialog like
`要允许"千问"拍摄照片和录制视频吗？`. AndroidBackend.dismiss_permission_popup
taps the most-permissive Allow using these tables. Constraints (enforced at
the call sites):
  * only fires when the FOREGROUND package is a known permission controller
    — protects against an in-app "允许" label triggering a spurious tap;
  * cheap foreground probe first (~200ms); the full a11y dump (~2.5s) is
    paid only when the probe says a permission UI is up;
  * capped per task; a stuck dialog won't infinite-loop;
  * env opt-out via RELAY_DISMISS_PERMISSIONS=0 (handled by the agent).

Extending for a new vendor/ROM: the defaults below stay in code (zero
startup I/O, zero behavior change when nothing is configured); a JSON
overlay can ADD entries without a code change:

    RELAY_VENDOR_PROFILE=/path/to/profile.json
    {"permission_packages": ["com.oem.grantor"],
     "allow_labels": ["总是同意"]}

Overlay packages are unioned; overlay labels are PREPENDED (they outrank
the defaults, since a vendor label is usually more specific).
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

from loguru import logger

_PROFILE_ENV = "RELAY_VENDOR_PROFILE"

PERMISSION_PACKAGES: tuple[str, ...] = (
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.lbe.security.miui",        # MIUI / Xiaomi
    "com.miui.securitycenter",
    "com.huawei.systemmanager",     # Huawei / Honor
    "com.coloros.safecenter",       # OPPO
    "com.heytap.openid",            # OPPO/realme newer
    "com.vivo.permissionmanager",   # vivo
    "com.samsung.android.permissioncontroller",
)

# Preference order: most-permissive first so e.g. "始终允许" wins over "允许".
ALLOW_LABELS: tuple[str, ...] = (
    "始终允许",
    "Always allow",
    "在使用应用时允许",
    "仅在使用该应用时允许",
    "使用该应用时允许",
    "While using the app",
    "Allow while using the app",
    "本次允许",
    "仅在本次使用允许",
    "Only this time",
    "允许",
    "Allow",
)


def _load_overlay() -> dict:
    path = os.getenv(_PROFILE_ENV)
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"{_PROFILE_ENV}={path!r} unreadable, ignoring overlay: {e}")
        return {}
    if not isinstance(data, dict):
        logger.warning(f"{_PROFILE_ENV}={path!r} is not a JSON object, ignoring overlay")
        return {}
    return data


@lru_cache(maxsize=1)
def permission_packages() -> tuple[str, ...]:
    """Defaults ∪ overlay (order: defaults first, new overlay entries after)."""
    extra = [p for p in _load_overlay().get("permission_packages") or []
             if isinstance(p, str) and p not in PERMISSION_PACKAGES]
    if extra:
        logger.info(f"vendor profile: +{len(extra)} permission package(s): {extra}")
    return PERMISSION_PACKAGES + tuple(extra)


@lru_cache(maxsize=1)
def allow_labels() -> tuple[str, ...]:
    """Overlay labels first (they outrank defaults), then the defaults."""
    extra = [s for s in _load_overlay().get("allow_labels") or []
             if isinstance(s, str) and s not in ALLOW_LABELS]
    if extra:
        logger.info(f"vendor profile: +{len(extra)} allow label(s): {extra}")
    return tuple(extra) + ALLOW_LABELS
