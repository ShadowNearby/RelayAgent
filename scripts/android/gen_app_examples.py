#!/usr/bin/env python3
"""Generate the bundled task-example catalog for the Android app.

Picks 50 example tasks from the benchmarks (all 30 RelayBench tasks +
20 spread across AndroidDaily scenarios) and writes them to
`android/app/src/main/res/raw/examples.json`, which ExamplesActivity reads
as quick-fill suggestions for the goal box.

    uv run python scripts/android/gen_app_examples.py
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
RELAYBENCH = REPO / "benchmark" / "relaybench_tasks.yaml"
ANDROIDDAILY = REPO / "benchmark" / "androiddaily_task_info.csv"
OUT = REPO / "android" / "app" / "src" / "main" / "res" / "raw" / "examples.json"

ANDROIDDAILY_PICKS = 20

# Package id -> human launcher label, for RelayBench single-app rows.
PKG_LABEL = {
    "com.aliyun.tongyi": "通义千问",
    "com.autonavi.minimap": "高德地图",
    "ctrip.android.view": "携程旅行",
    "com.google.android.apps.bard": "Gemini",
    "com.tencent.mm": "微信",
    "com.xingin.xhs": "小红书",
    "cn.wps.moffice_eng": "WPS",
    "com.booking": "Booking.com",
    "com.microsoft.copilot": "Copilot",
    "com.reddit.frontpage": "Reddit",
}


def _relaybench() -> list[dict]:
    data = yaml.safe_load(RELAYBENCH.read_text(encoding="utf-8"))
    out = []
    for t in data["tasks"]:
        cross = t.get("task_type") == "cross_app"
        if cross:
            app = "跨 App"
        else:
            app = PKG_LABEL.get(t.get("app", ""), t.get("app", ""))
        out.append(
            {
                "id": t["id"],
                "instruction": t["instruction"],
                "app": app,
                "category": t.get("category", ""),
                "difficulty": t.get("difficulty", ""),
                "type": "cross_app" if cross else "single_app",
                "source": "RelayBench",
            }
        )
    return out


def _androiddaily(n: int) -> list[dict]:
    rows = list(csv.DictReader(ANDROIDDAILY.read_text(encoding="utf-8").splitlines()))
    # "场景" is the real category column; "类别" ships empty in this CSV.
    by_scene: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        instr = (r.get("任务") or "").strip().strip('"')
        if not instr:
            continue
        # Prefer self-contained, no-precondition, lower-effort rows as examples.
        if (r.get("需要预制") or "0").strip() != "0":
            continue
        if (r.get("综合难度") or "").strip() == "hard":
            continue
        by_scene[(r.get("场景") or "其他").strip()].append(
            {
                "id": f"ad-{r.get('task_tag', '').strip()}",
                "instruction": instr,
                "app": (r.get("APP名称") or "").strip(),
                "category": (r.get("场景") or "").strip(),
                "difficulty": (r.get("综合难度") or "").strip(),
                "type": "single_app",
                "source": "AndroidDaily",
            }
        )
    # Round-robin across scenes for a diverse spread, deterministic order.
    scenes = sorted(by_scene)
    picked: list[dict] = []
    idx = 0
    while len(picked) < n and any(by_scene.values()):
        scene = scenes[idx % len(scenes)]
        bucket = by_scene[scene]
        if bucket:
            picked.append(bucket.pop(0))
        idx += 1
        if idx > len(scenes) * 50:  # safety against an empty-everything spin
            break
    return picked[:n]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT,
        help=f"Output path (default: {OUT.relative_to(REPO)})",
    )
    args = parser.parse_args()
    out = args.out
    examples = _relaybench() + _androiddaily(ANDROIDDAILY_PICKS)
    payload = {
        "version": 1,
        "generated": date.today().isoformat(),
        "count": len(examples),
        "examples": examples,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    shown = out.relative_to(REPO) if out.is_relative_to(REPO) else out
    print(f"wrote {len(examples)} examples -> {shown}")


if __name__ == "__main__":
    main()
