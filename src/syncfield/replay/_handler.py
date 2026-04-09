"""Internal helpers for the replay HTTP server.

Right now this is just :func:`safe_resolve` — the path-traversal guard
that every file-serving route must funnel through. Kept in its own
module so the security-sensitive surface is small and easy to audit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


class UnsafePathError(ValueError):
    """Raised when a requested path resolves outside the session root."""


def safe_resolve(root: Path, requested: str) -> Optional[Path]:
    """Resolve ``requested`` against ``root`` or refuse.

    Returns the resolved absolute path if it exists and is contained
    inside ``root`` (after following symlinks). Returns ``None`` if the
    path simply does not exist. Raises :class:`UnsafePathError` if the
    request tries to escape the root in any way — absolute paths,
    parent traversals, and symlinks pointing outside all qualify.
    """
    if requested.startswith("/") or requested.startswith("\\"):
        raise UnsafePathError(f"absolute path rejected: {requested!r}")

    root_abs = root.resolve(strict=True)
    candidate = (root_abs / requested).resolve(strict=False)

    try:
        candidate.relative_to(root_abs)
    except ValueError as exc:
        raise UnsafePathError(
            f"path escapes session root: {requested!r}"
        ) from exc

    if not candidate.exists():
        return None
    return candidate
