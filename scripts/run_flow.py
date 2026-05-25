#!/usr/bin/env python3
"""Run a multi-app flow YAML via FlowRunner.

Each step is either:
  - app_step: cold-launch the target app and run one `mw test` sub-process
    pinned to a single capability_id (router is bypassed). The in-app
    agent's reply is captured and bound to the flow blackboard.
  - ask_user: prompt on stdin; supports `select_from` for picking out of
    a previously bound array.
  - extract (inside an app_step): run a small text LLM call against the
    captured reply to parse structured data.

Usage:
    scripts/run_flow.py manifests/_flows/xhs_to_amap_coffee.yaml
    scripts/run_flow.py manifests/_flows/xhs_to_amap_coffee.yaml \\
        --input topic="上海安福路咖啡" --input max_choices=3 -- --max-step 60

Any args after a literal `--` are forwarded to each underlying `mw test`.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.flow_runner import main

if __name__ == "__main__":
    sys.exit(main())
