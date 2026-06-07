"""Start a fresh 通义千问 conversation on the connected device.

Used as a controlled pre-step for the a11y-text-input baseline (§8.9): every
re-driving run starts from the same clean input-box state general_e2e faced,
so the only variable measured is the input modality. NOT part of any agent —
it is harness-side state setup.

Usage:  uv run python benchmark/fresh_conv.py [com.aliyun.tongyi]
Assumes the app is already foreground (call after cold_launch).
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents._adb import adb_base, _get_screen_size  # noqa: E402
from agents.relay_agent import _ground_text_via_uiautomator  # noqa: E402


def _tap(x: int, y: int) -> None:
    subprocess.run(adb_base() + ["shell", "input", "tap", str(x), str(y)],
                   capture_output=True, text=True, timeout=10)


def fresh_conversation_tongyi() -> bool:
    w, h = _get_screen_size()
    # 1) open the left drawer (hamburger, top-left) — card x_prepare_fresh_conversation
    _tap(int(0.075 * w), int(0.10 * h))
    time.sleep(0.8)
    # 2) tap 新建对话
    hit = _ground_text_via_uiautomator("新建对话", w, h)
    if hit is None:
        # drawer may already be a fresh chat, or label differs; try 新对话
        hit = _ground_text_via_uiautomator("新对话", w, h)
    if hit is not None:
        _tap(*hit)
        time.sleep(0.8)
    # 3) settle; verify the input placeholder is present
    ok = _ground_text_via_uiautomator("发消息或按住说话", w, h) is not None
    print(f"fresh_conversation_tongyi: tapped new-chat={hit is not None}, "
          f"input-box-present={ok}")
    return ok


if __name__ == "__main__":
    pkg = sys.argv[1] if len(sys.argv) > 1 else "com.aliyun.tongyi"
    if pkg != "com.aliyun.tongyi":
        sys.exit(f"fresh_conv only implemented for com.aliyun.tongyi (got {pkg})")
    sys.exit(0 if fresh_conversation_tongyi() else 1)
