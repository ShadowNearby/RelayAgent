"""Validate every card in manifests/ against the normative spec.

Three layers, all device-less and LLM-less (CI-safe):

1. JSON-Schema validation against `spec/schema.json` (the normative mirror of
   SPEC.md — unknown top-level keys, missing required fields, bad selector
   shapes all fail here). Runs with a FormatChecker so `format: date` asserts.
2. Cross-field checks JSON Schema cannot express: manifest filename matches
   `<app_id>.yaml`, and `provenance.verified_os` names a platform listed in
   `platforms`.
3. Catalog build (`agents.card_catalog.build_catalog`), which adds the
   load-time `prompt_template` / `prompt_slots` consistency checks and
   capability-id uniqueness.

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


def cross_field_errors(path: Path, doc: dict) -> list[str]:
    """Card-level consistency rules JSON Schema cannot express (layer 2)."""
    errs = []
    app_id = doc.get("app_id")
    if app_id and path.name != f"{app_id}.yaml":
        errs.append(
            f"filename {path.name!r} does not match app_id ({app_id}.yaml expected)"
        )
    verified_os = (doc.get("provenance") or {}).get("verified_os") or ""
    platforms = doc.get("platforms") or []
    os_name = verified_os.split("-", 1)[0]
    if os_name and platforms and os_name not in platforms:
        errs.append(
            f"provenance.verified_os {verified_os!r} names a platform "
            f"not listed in platforms {platforms!r}"
        )
    return errs


def main(argv: list[str]) -> int:
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    # Honor the draft the schema itself declares (draft-07 today). The
    # FormatChecker makes `format: date` assert instead of being annotation-only
    # (the schema also carries a pattern as a belt-and-suspenders fallback).
    validator = jsonschema.validators.validator_for(schema)(
        schema, format_checker=jsonschema.FormatChecker()
    )

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
        extra = cross_field_errors(path, doc) if isinstance(doc, dict) else []
        if errors or extra:
            failures += 1
            print(f"✗ {path.name}")
            for e in errors:
                loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
                print(f"    {loc}: {e.message}")
            for msg in extra:
                print(f"    {msg}")
        else:
            print(f"✓ {path.name}")

    # Layer 3: catalog build (prompt_template consistency, capability-id
    # uniqueness).
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
