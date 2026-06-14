#!/usr/bin/env python3
"""Spike B: diff an app-side a11y dump against a real uiautomator dump.

The Android app's A11yXmlSerializer must be node-equivalent to
`adb shell uiautomator dump` for the attributes relay_agent.py actually
consumes: text, content-desc, bounds (grounding / reply scrape / text-hash),
plus package + clickable (permission-popup scan). This script compares the
two dumps of the SAME screen and reports per-attribute-set differences.

Usage:
    # 1. device side, same screen, two captures back to back:
    adb shell uiautomator dump /sdcard/ua.xml && adb pull /sdcard/ua.xml
    #    (app side: trigger uiDumpXml() via the app's debug surface and pull
    #     the file, or paste from logcat)
    # 2. host:
    uv run python scripts/android/diff_a11y_dump.py ua.xml app.xml

Exit code: 0 when the reply-relevant node sets match, 1 otherwise.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _nodes(path: Path) -> list[dict]:
    root = ET.parse(path).getroot()
    out = []
    for n in root.iter("node"):
        out.append({
            "text": (n.get("text") or "").strip(),
            "desc": (n.get("content-desc") or "").strip(),
            "bounds": n.get("bounds") or "",
            "package": n.get("package") or "",
            "clickable": (n.get("clickable") or "").lower() == "true",
        })
    return out


def _key_text(nodes: list[dict]) -> set[tuple]:
    """The reply-scrape view: every (text|desc, bounds) pair that carries text."""
    keys = set()
    for n in nodes:
        if n["text"]:
            keys.add(("text", n["text"], n["bounds"]))
        if n["desc"] and n["desc"] != n["text"]:
            keys.add(("desc", n["desc"], n["bounds"]))
    return keys


def _key_text_only(nodes: list[dict]) -> list[str]:
    """Document-order text stream — what _dump_visible_text_hash hashes."""
    parts = []
    for n in nodes:
        if n["text"]:
            parts.append(n["text"])
        if n["desc"] and n["desc"] != n["text"]:
            parts.append(n["desc"])
    return parts


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("uiautomator_xml", type=Path, help="dump from `uiautomator dump`")
    p.add_argument("app_xml", type=Path, help="dump from the app's A11yXmlSerializer")
    p.add_argument("--loose-bounds", action="store_true",
                   help="Compare text presence only, ignoring bounds (use when "
                        "the two captures are seconds apart and content scrolled)")
    args = p.parse_args()

    ua = _nodes(args.uiautomator_xml)
    app = _nodes(args.app_xml)
    print(f"uiautomator: {len(ua)} nodes; app: {len(app)} nodes")

    if args.loose_bounds:
        ua_keys = {k[:2] for k in _key_text(ua)}
        app_keys = {k[:2] for k in _key_text(app)}
    else:
        ua_keys = _key_text(ua)
        app_keys = _key_text(app)

    missing = ua_keys - app_keys   # in uiautomator, absent from app dump
    extra = app_keys - ua_keys     # app-only

    if missing:
        print(f"\n❌ {len(missing)} node(s) the app dump MISSES (breaks scrape/grounding):")
        for k in sorted(missing)[:40]:
            print(f"  - {k}")
    if extra:
        print(f"\nℹ️ {len(extra)} app-only node(s) (usually harmless — extra windows):")
        for k in sorted(extra)[:20]:
            print(f"  + {k}")

    ua_stream = _key_text_only(ua)
    app_stream = _key_text_only(app)
    same_stream = ua_stream == app_stream
    print(f"\ntext-hash stream identical: {same_stream} "
          f"(ua {len(ua_stream)} parts / app {len(app_stream)} parts)")
    if not same_stream:
        print("  (order or content differs — wait_for_reply text-hash would diverge; "
            "diff the streams above)")

    ok = not missing
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
