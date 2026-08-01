#!/usr/bin/env python3
"""Increment the SyncField SemVer stored in pyproject.toml."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

VERSION_PATTERN = re.compile(
    r'(?m)^version = "(?P<major>0|[1-9]\d*)\.'
    r'(?P<minor>0|[1-9]\d*)\.'
    r'(?P<patch>0|[1-9]\d*)"$'
)
PROJECT_SECTION_PATTERN = re.compile(
    r"(?ms)^\[project\]\s*$.*?(?=^\[|\Z)"
)
MAJOR_TITLE_PATTERN = re.compile(r"^major(?:[!: ]|$)", re.IGNORECASE)
FEAT_TITLE_PATTERN = re.compile(r"^feat(?:\([^)]*\))?!?:", re.IGNORECASE)


def bump_kind_for_title(title: str) -> str:
    normalized = title.strip()
    if MAJOR_TITLE_PATTERN.match(normalized):
        return "major"
    if FEAT_TITLE_PATTERN.match(normalized):
        return "minor"
    return "patch"


def bump_version(pyproject: Path, bump: str) -> str:
    text = pyproject.read_text(encoding="utf-8")
    project_sections = list(PROJECT_SECTION_PATTERN.finditer(text))
    if len(project_sections) != 1:
        raise ValueError(f"expected exactly one [project] section in {pyproject}")

    project_section = project_sections[0]
    matches = list(VERSION_PATTERN.finditer(project_section.group()))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one [project] version in {pyproject}")

    match = matches[0]
    major, minor, patch = (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    elif bump == "patch":
        patch += 1
    else:
        raise ValueError(f"unsupported bump: {bump}")

    version = f"{major}.{minor}.{patch}"
    start = project_section.start() + match.start()
    end = project_section.start() + match.end()
    updated = text[:start] + f'version = "{version}"' + text[end:]
    pyproject.write_text(updated, encoding="utf-8")
    return version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bump", nargs="?", choices=("major", "minor", "patch"))
    parser.add_argument("--title")
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "pyproject.toml",
    )
    args = parser.parse_args()
    if (args.bump is None) == (args.title is None):
        parser.error("provide exactly one of bump or --title")
    bump = args.bump
    if bump is None:
        assert args.title is not None
        bump = bump_kind_for_title(args.title)
    print(bump_version(args.pyproject, bump))


if __name__ == "__main__":
    main()
