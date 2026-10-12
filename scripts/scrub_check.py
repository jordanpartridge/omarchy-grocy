#!/usr/bin/env python3
"""Fail if personal hosts, locations, or live credentials are still in the tree."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", ".ruff_cache"}
NAME_J = "jor" + "dan"
NAME_P = "part" + "ridge"
COPYRIGHT = NAME_J[:1].upper() + NAME_J[1:] + " " + NAME_P[:1].upper() + NAME_P[1:]
REPO = "github.com/" + NAME_J + NAME_P + "/omarchy-grocy"
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ALLOWED_IPV4 = {
    "127.0.0.1",
    "100.64.0.0",
    "100.64.1.2",
    "10.1.2.3",
    "8.8.8.8",
    "169.254.169.254",
}
FORBIDDEN = (
    "lion-" + "balance",
    ".ts." + "net",
    "grocy-" + "piper",
    "/home/" + NAME_J,
    "signal " + "butte",
    "ells" + "worth",
    "852" + "09",
    "852" + "08",
)
WORD = (
    re.compile(r"\bod" + r"in\b", re.IGNORECASE),
    re.compile(r"\bth" + r"or\b", re.IGNORECASE),
    re.compile(r"\bme" + r"sa\b", re.IGNORECASE),
)
PLUGIN_TOKENS = (NAME_J + ".grocy", NAME_J + "-grocy", NAME_J + "_grocy")
LIVE_KEY = re.compile(
    r"(?i)^(?!\s*#).*(?:"
    r"GROCY_API_KEY\s*=\s*['\"]?[0-9a-fA-F]{32}"
    r"|KROGER_CLIENT_SECRET\s*=\s*['\"]?[A-Za-z0-9_\-]{16}"
    r"|KROGER_CLIENT_ID\s*=\s*['\"]?[A-Za-z0-9_\-]{8}"
    r"|KROGER_LOCATION_ID\s*=\s*['\"]?[0-9]{8}"
    r")"
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def files() -> list[Path]:
    found: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            found.append(path)
    return found


def strip_plugin_ids(text: str) -> str:
    text = text.replace(REPO, "")
    text = text.replace(COPYRIGHT, "")
    for token in PLUGIN_TOKENS:
        text = text.replace(token, "")
    return text


def main() -> int:
    problems: list[str] = []
    for path in files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            problems.append(f"{path.relative_to(ROOT)}: not UTF-8 text")
            continue
        rel = str(path.relative_to(ROOT))
        lowered = text.lower()
        for token in FORBIDDEN:
            if token in lowered:
                problems.append(f"{rel}: forbidden marker")
        for pattern in WORD:
            if pattern.search(text):
                problems.append(f"{rel}: forbidden name")
        for match in IPV4.findall(text):
            if match not in ALLOWED_IPV4:
                problems.append(f"{rel}: unexpected IPv4 {match}")
        if NAME_P in lowered and COPYRIGHT not in text and REPO not in text:
            problems.append(f"{rel}: owner name outside the copyright line and repository URL")
        if rel not in {"LICENSE", "manifest.json", "README.md"} and (NAME_P in lowered or COPYRIGHT in text):
            problems.append(f"{rel}: owner name is only expected in LICENSE, manifest.json, and README.md")
        if re.search(NAME_J, strip_plugin_ids(text), re.IGNORECASE):
            problems.append(f"{rel}: given name outside the plugin id and repository URL")
        if rel != "tests/test_grocy.py":
            for line_no, line in enumerate(text.splitlines(), 1):
                if LIVE_KEY.search(line):
                    problems.append(f"{rel}:{line_no}: credential assignment is not empty or commented")
        for match in EMAIL.findall(text):
            if match.endswith("@grocy.example"):
                continue
            problems.append(f"{rel}: email-like value")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("scrub check ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
