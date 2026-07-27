#!/usr/bin/env python3
"""Validate benchmark/relaybench_tasks.yaml structure and balance constraints."""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.routing.card_catalog import build_catalog

TASKS_PATH = REPO_ROOT / "benchmark" / "relaybench_tasks.yaml"


def _catalog_maps() -> tuple[set[str], dict[str, set[str]], dict[str, str]]:
    catalog = build_catalog()
    app_ids: set[str] = set()
    cap_to_apps: dict[str, set[str]] = {}
    app_names: dict[str, str] = {}
    for doc in catalog["apps"]:
        aid = doc["app_id"]
        app_ids.add(aid)
        app_names[aid] = doc.get("app_name") or aid
        for cap in doc.get("capabilities") or []:
            cid = cap.get("id")
            if cid:
                cap_to_apps.setdefault(cid, set()).add(aid)
    return app_ids, cap_to_apps, app_names


def _labels(task: dict, app_names: dict[str, str]) -> dict[str, str]:
    raw = task.get("app_labels") or {}
    apps = task.get("apps") or []
    out = {a: raw.get(a) or app_names.get(a, a) for a in apps}
    return out


def validate(path: Path = TASKS_PATH) -> list[str]:
    errors: list[str] = []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tasks = doc.get("tasks") or []
    if len(tasks) != 30:
        errors.append(f"expected 30 tasks, got {len(tasks)}")

    app_ids, cap_to_apps, app_names = _catalog_maps()
    ids_seen: set[str] = set()
    all_apps: Counter[str] = Counter()
    cross_apps: Counter[str] = Counter()
    singles = crosses = 0

    for task in tasks:
        tid = task.get("id")
        if not tid:
            errors.append(f"task missing id: {task!r}")
            continue
        if tid in ids_seen:
            errors.append(f"duplicate id: {tid}")
        ids_seen.add(tid)

        ttype = task.get("task_type")
        apps = task.get("apps") or []
        caps = task.get("capabilities") or []
        instruction = task.get("instruction") or ""

        if ttype == "single_app":
            singles += 1
            if len(apps) != 1:
                errors.append(f"{tid}: single_app must have exactly one app")
        elif ttype == "cross_app":
            crosses += 1
            if len(apps) < 2:
                errors.append(f"{tid}: cross_app needs >=2 apps")
        else:
            errors.append(f"{tid}: unknown task_type {ttype!r}")

        for a in apps:
            if a not in app_ids:
                errors.append(f"{tid}: unknown app {a}")
            all_apps[a] += 1
            if ttype == "cross_app":
                cross_apps[a] += 1

        if len(apps) != len(caps):
            # This is a validation error, not a crash: a task that lists apps
            # but forgets capabilities (or vice versa) must show up as an
            # error line, and an empty list must not IndexError the tool.
            errors.append(
                f"{tid}: apps ({len(apps)}) / capabilities ({len(caps)}) length "
                f"mismatch — each leg needs one app + one capability"
            )
        legs = list(zip(apps, caps))  # zip truncates on mismatch; validate what pairs exist
        for a, cap in legs:
            if cap not in cap_to_apps:
                errors.append(f"{tid}: unknown capability {cap}")
                continue
            if a not in cap_to_apps[cap]:
                errors.append(f"{tid}: {a} does not declare capability {cap}")

        labels = _labels(task, app_names)
        if ttype == "cross_app":
            for a, label in labels.items():
                if label not in instruction:
                    errors.append(f"{tid}: instruction missing app label {label!r} for {a}")

        for a, cap in legs:
            if len(cap_to_apps.get(cap, ())) < 2:
                continue
            label = labels.get(a) or app_names.get(a, a)
            if label not in instruction:
                errors.append(
                    f"{tid}: ambiguous capability {cap} requires label {label!r} in instruction"
                )

    if singles != 15:
        errors.append(f"expected 15 single_app tasks, got {singles}")
    if crosses != 15:
        errors.append(f"expected 15 cross_app tasks, got {crosses}")

    if cross_apps and any(v != 3 for v in cross_apps.values()):
        errors.append(f"cross_app app balance not 3 each: {dict(cross_apps)}")
    if all_apps and max(all_apps.values()) - min(all_apps.values()) > 1:
        errors.append(f"overall app balance max-min > 1: {dict(all_apps)}")

    return errors


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else TASKS_PATH
    errors = validate(path)
    if errors:
        print(f"FAIL {path} ({len(errors)} error(s)):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"OK {path} — 30 tasks, app balance valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
