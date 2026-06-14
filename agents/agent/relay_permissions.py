"""System permission popup auto-dismiss for RelayAgent.

The logic and vendor tables (permission-controller packages, Allow labels)
live in the device backend (agents/device/android.py +
agents/device/vendor_profiles.py); RelayAgent hooks this at the top of every
predict so the planner doesn't have to know anything about runtime-permission
dialogs. Capped per task; env opt-out via RELAY_DISMISS_PERMISSIONS=0.
"""

from __future__ import annotations

from agents.device import get_backend


def _maybe_dismiss_permission_popup() -> str | None:
    """If a system permission/consent dialog is on top, tap the most-permissive
    Allow button. Returns the label tapped (for logging) or None when nothing
    was dismissed."""
    return get_backend().dismiss_permission_popup()
