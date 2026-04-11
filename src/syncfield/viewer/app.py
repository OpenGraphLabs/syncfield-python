"""Web viewer application — FastAPI + uvicorn + browser launcher.

Replaces the DearPyGui desktop viewer with a browser-based UI. The
public API (:func:`launch` / :func:`launch_passive`) has the same
signature so user scripts are unchanged.

Thread model:

- **Main thread** (blocking mode): runs ``uvicorn.run()``.
- **Background thread** (passive mode): runs uvicorn via a separate
  ``asyncio`` event loop so the caller keeps control.
- **Poller thread** (daemon): same as before — 10 Hz snapshot polling.
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from contextlib import contextmanager
from typing import Iterator, Optional

import uvicorn

from syncfield.orchestrator import SessionOrchestrator
from syncfield.viewer.poller import SessionPoller
from syncfield.viewer.server import ViewerServer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handle returned from launch_passive()
# ---------------------------------------------------------------------------


class ViewerHandle:
    """Minimal handle exposed by :func:`launch_passive`.

    Mirrors the shape of the previous DearPyGui handle so callers
    coming from the desktop viewer have zero friction.
    """

    def __init__(self, app: "ViewerApp") -> None:
        self._app = app

    def is_running(self) -> bool:
        """Return True while the web server is still running."""
        return self._app.is_running()

    def close(self) -> None:
        """Stop the web server and the poller."""
        self._app.close()


# ---------------------------------------------------------------------------
# Public launchers
# ---------------------------------------------------------------------------


def launch(
    session: SessionOrchestrator,
    *,
    host: str = "127.0.0.1",
    port: int = 8420,
    title: str = "SyncField",
) -> None:
    """Start the web viewer and block until Ctrl+C.

    Opens a browser tab pointing at the viewer. In blocking mode the
    viewer *owns* the session lifecycle — the user clicks Record / Stop
    in the browser, and the server dispatches the corresponding
    ``SessionOrchestrator`` methods.

    Args:
        session: The orchestrator to observe and control.
        host: Bind address. Default ``"127.0.0.1"`` (localhost only).
        port: Bind port. Default ``8420``.
        title: Browser tab title. Default ``"SyncField"``.
    """
    app = ViewerApp(session, host=host, port=port, title=title)
    try:
        app.setup()
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()


@contextmanager
def launch_passive(
    session: SessionOrchestrator,
    *,
    host: str = "127.0.0.1",
    port: int = 8420,
    title: str = "SyncField",
) -> Iterator[ViewerHandle]:
    """Open the viewer in **passive** mode and return a handle.

    Use this when the caller owns the session lifecycle — e.g. a script
    that wants the web UI as an observer while it runs its own start/stop
    logic. The web server runs on a background thread so the caller keeps
    control of the main thread.

    Example::

        with syncfield.viewer.launch_passive(session) as viewer:
            session.start()
            while viewer.is_running():
                time.sleep(0.1)
            session.stop()
    """
    app = ViewerApp(session, host=host, port=port, title=title)
    app.setup()

    bg_thread = threading.Thread(
        target=app.run, name="syncfield-viewer-passive", daemon=True
    )
    bg_thread.start()

    try:
        yield ViewerHandle(app)
    finally:
        app.close()
        bg_thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Core app class
# ---------------------------------------------------------------------------


class ViewerApp:
    """Owns the FastAPI server and uvicorn lifecycle for one viewer session."""

    def __init__(
        self,
        session: SessionOrchestrator,
        *,
        host: str = "127.0.0.1",
        port: int = 8420,
        title: str = "SyncField",
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._title = title
        self._poller = SessionPoller(session)
        self._server: Optional[ViewerServer] = None
        self._uvicorn_server: Optional[uvicorn.Server] = None
        self._running = False
        self._setup_done = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Create the FastAPI app and start the poller."""
        if self._setup_done:
            return
        self._server = ViewerServer(
            self._session, self._poller, title=self._title,
        )
        self._poller.start()
        self._setup_done = True

    def run(self) -> None:
        """Run uvicorn on the calling thread (blocking).

        Opens a browser tab after a short delay to let the server bind.
        """
        if not self._setup_done:
            self.setup()

        assert self._server is not None

        # Open browser in a background thread after a short delay
        url = f"http://{self._host}:{self._port}"
        threading.Thread(
            target=self._open_browser, args=(url,), daemon=True,
        ).start()

        self._running = True
        config = uvicorn.Config(
            app=self._server.app,
            host=self._host,
            port=self._port,
            log_level="warning",
        )
        self._uvicorn_server = uvicorn.Server(config)
        try:
            self._uvicorn_server.run()
        finally:
            self._running = False

    def close(self) -> None:
        """Stop uvicorn, the poller, and tear down the session."""
        # Signal uvicorn to shut down
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True

        self._poller.stop()
        self._running = False
        self._setup_done = False

    def is_running(self) -> bool:
        return self._running

    @staticmethod
    def _open_browser(url: str) -> None:
        """Open the viewer URL in the default browser after a brief settle."""
        import time
        time.sleep(0.8)
        try:
            webbrowser.open(url)
        except Exception:
            logger.debug("Could not open browser — visit %s manually", url)
