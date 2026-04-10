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

import logging
import threading
import time
import webbrowser
from pathlib import Path
from typing import Union

from syncfield.replay.loader import load_session
from syncfield.replay.server import ReplayServer

logger = logging.getLogger(__name__)

__all__ = ["launch"]


def launch(
    session_dir: Union[str, Path],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """Open a synced-session replay viewer in the default browser.

    Args:
        session_dir: Path to a session folder written by
            :class:`syncfield.SessionOrchestrator`.
        host: Bind interface. Defaults to localhost; do not change
            this without understanding that there is no auth layer.
        port: TCP port. ``0`` picks an ephemeral free port.
        open_browser: If True (default), open the user's default
            browser to the served URL once the server is ready.

    The function blocks the calling thread until the server stops.
    Press Ctrl+C to terminate.
    """
    manifest = load_session(Path(session_dir))
    server = ReplayServer(manifest, host=host, port=port)

    if open_browser:
        def _open_after_ready() -> None:
            # Poll uvicorn's `started` flag so we wait for the real
            # bind to complete — for port=0, reading server.url before
            # startup would give us http://127.0.0.1:0/ which is broken.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if getattr(server._server, "started", False):
                    break
                time.sleep(0.02)
            try:
                webbrowser.open(server.url)
            except Exception:
                logger.warning("could not open browser", exc_info=True)

        threading.Thread(
            target=_open_after_ready,
            name="replay-browser-launcher",
            daemon=True,
        ).start()

    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("replay server interrupted, shutting down")
