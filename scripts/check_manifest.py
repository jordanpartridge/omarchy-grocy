#!/usr/bin/env python3
"""Validate manifest.json for the jordan.grocy bar widget."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "schemaVersion",
    "id",
    "name",
    "version",
    "author",
    "license",
    "description",
    "kinds",
    "entryPoints",
    "barWidget",
)
BAR_REQUIRED = (
    "displayName",
    "description",
    "category",
    "aliases",
    "allowMultiple",
    "defaultSection",
)


def main() -> int:
    path = ROOT / "manifest.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"manifest.json is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(doc, dict):
        print("manifest.json must be an object", file=sys.stderr)
        return 1
    missing = [key for key in REQUIRED if key not in doc]
    if missing:
        print(f"manifest.json missing {', '.join(missing)}", file=sys.stderr)
        return 1
    if doc["schemaVersion"] != 1 or doc["id"] != "jordan.grocy":
        print("manifest.json schemaVersion or id is unexpected", file=sys.stderr)
        return 1
    if doc["license"] != "MIT" or not isinstance(doc["version"], str) or not doc["version"]:
        print("manifest.json license or version is unexpected", file=sys.stderr)
        return 1
    if doc["kinds"] != ["bar-widget"]:
        print("manifest.json kinds must be [\"bar-widget\"]", file=sys.stderr)
        return 1
    entry = doc["entryPoints"].get("barWidget") if isinstance(doc["entryPoints"], dict) else None
    if not isinstance(entry, str) or not (ROOT / entry).is_file():
        print("manifest.json bar widget entry point is missing", file=sys.stderr)
        return 1
    bar = doc["barWidget"]
    if not isinstance(bar, dict):
        print("manifest.json barWidget must be an object", file=sys.stderr)
        return 1
    absent = [key for key in BAR_REQUIRED if key not in bar]
    if absent:
        print(f"manifest.json barWidget missing {', '.join(absent)}", file=sys.stderr)
        return 1
    if not isinstance(bar["aliases"], list) or not all(isinstance(item, str) and item for item in bar["aliases"]):
        print("manifest.json aliases must be a non-empty list of strings", file=sys.stderr)
        return 1
    if bar["allowMultiple"] is not False or bar["defaultSection"] not in {"left", "center", "right"}:
        print("manifest.json bar placement is unexpected", file=sys.stderr)
        return 1
    print("manifest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
