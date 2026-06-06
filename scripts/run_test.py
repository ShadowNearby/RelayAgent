#!/usr/bin/env python3
"""Deprecated shim — forwards to scripts/run_native.py.

The single-app entry is now `scripts/run_native.py`, which drives the agent
over direct adb. This shim preserves the old invocation so existing muscle
memory / docs keep working:

    scripts/run_test.py com.aliyun.tongyi "帮我点三杯蜜雪冰城蜜桃四季春"

It simply re-execs run_native.py with the same argv. Prefer calling
run_native.py directly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_NATIVE = REPO_ROOT / "scripts" / "run_native.py"


def main() -> int:
    sys.stderr.write(
        "▶ run_test.py is deprecated; forwarding to run_native.py "
        "(direct adb).\n"
    )
    os.execv(sys.executable, [sys.executable, str(RUN_NATIVE), *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
