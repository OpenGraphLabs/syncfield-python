from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bump_version.py"
SPEC = importlib.util.spec_from_file_location("bump_version", SCRIPT)
assert SPEC and SPEC.loader
bump_version = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bump_version)


def test_pr_title_selects_semver_bump() -> None:
    assert bump_version.bump_kind_for_title("major: break protocol") == "major"
    assert bump_version.bump_kind_for_title("feat: add adapter") == "minor"
    assert bump_version.bump_kind_for_title("feat(oak)!: replace API") == "minor"
    assert bump_version.bump_kind_for_title("fix: reconnect camera") == "patch"
    assert bump_version.bump_kind_for_title("ci: update workflow") == "patch"


def test_bump_version_updates_only_project_version(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nversion = "1.2.3"\n\n[tool.example]\nversion = "9.9.9"\n',
        encoding="utf-8",
    )

    assert bump_version.bump_version(pyproject, "minor") == "1.3.0"
    assert 'version = "1.3.0"' in pyproject.read_text(encoding="utf-8")
    assert 'version = "9.9.9"' in pyproject.read_text(encoding="utf-8")
