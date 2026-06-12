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
"""
from __future__ import annotations

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
