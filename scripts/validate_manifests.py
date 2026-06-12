"""Validate every card in manifests/ against the normative spec.

Two layers, both device-less and LLM-less (CI-safe):

1. JSON-Schema validation against `spec/schema.json` (the normative mirror of
   SPEC.md — unknown top-level keys, missing required fields, bad selector
   shapes all fail here).
2. Catalog build (`agents.card_catalog.build_catalog`), which adds the
   load-time `prompt_template` / `prompt_slots` consistency checks.

Usage:
    uv run python scripts/validate_manifests.py [manifest.yaml ...]

With no arguments validates all of `manifests/*.yaml` (skipping `_`-prefixed
entries such as `_generated/`). Exit code 0 = all valid.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCHEMA_PATH = ROOT / "spec" / "schema.json"
MANIFEST_DIR = ROOT / "manifests"


def main(argv: list[str]) -> int:
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    # Honor the draft the schema itself declares (draft-07 today).
    validator = jsonschema.validators.validator_for(schema)(schema)

    if argv:
        files = [Path(a) for a in argv]
    else:
        files = sorted(p for p in MANIFEST_DIR.glob("*.yaml") if not p.name.startswith("_"))
    if not files:
        print("no manifests found")
        return 1

    failures = 0
    for path in files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
        if errors:
            failures += 1
            print(f"✗ {path.name}")
            for e in errors:
                loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
                print(f"    {loc}: {e.message}")
        else:
            print(f"✓ {path.name}")

    # Layer 2: catalog build (prompt_template consistency, app_id checks).
    from agents.card_catalog import ManifestValidationError, build_catalog

    try:
        catalog = build_catalog()
        n_caps = sum(len(a["capabilities"]) for a in catalog["apps"])
        print(f"✓ catalog builds: {len(catalog['apps'])} apps, {n_caps} capabilities")
    except ManifestValidationError as e:
        failures += 1
        print(f"✗ catalog build failed:\n{e}")

    if failures:
        print(f"\n{failures} failure(s)")
        return 1
    print("\nall manifests valid")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
