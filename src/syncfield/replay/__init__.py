"""SyncField replay viewer — local browser-based session playback.

Open a previously recorded session in your default browser to verify
sync quality (per-stream offsets, Before/After comparison, sync report).

Usage::

    import syncfield as sf
    sf.replay.launch("./data/session_2026-04-09T14-49")

Requires the ``replay`` extra::

    pip install 'syncfield[replay]'
"""

from __future__ import annotations

try:
    import starlette  # noqa: F401
    import uvicorn  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised at import time on CI
    raise ImportError(
        "syncfield.replay requires the 'replay' extra. "
        "Install with `pip install 'syncfield[replay]'`."
    ) from exc

# Re-exported once server.py exists in Task 5.
__all__ = ["launch"]
