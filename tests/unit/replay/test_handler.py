"""Path-traversal protection for the media/data routes."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncfield.replay._handler import UnsafePathError, safe_resolve


def test_safe_resolve_accepts_in_root(tmp_path: Path) -> None:
    target = tmp_path / "ok.mp4"
    target.write_bytes(b"x")
    assert safe_resolve(tmp_path, "ok.mp4") == target.resolve()


def test_safe_resolve_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        safe_resolve(tmp_path, "../etc/passwd")


def test_safe_resolve_rejects_absolute(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        safe_resolve(tmp_path, "/etc/passwd")


def test_safe_resolve_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_target"
    outside.write_text("secret")
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(UnsafePathError):
        safe_resolve(tmp_path, "link")
    outside.unlink()


def test_safe_resolve_missing_file_returns_none(tmp_path: Path) -> None:
    assert safe_resolve(tmp_path, "nope.mp4") is None
