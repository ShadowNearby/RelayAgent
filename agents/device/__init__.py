"""Device backend package — see base.py for the interface contract."""
from agents.device.base import DeviceBackend, Key, RecordingHandle, UINode
from agents.device.factory import current_platform, get_backend, set_default_backend

__all__ = [
    "DeviceBackend",
    "Key",
    "RecordingHandle",
    "UINode",
    "current_platform",
    "get_backend",
    "set_default_backend",
]
