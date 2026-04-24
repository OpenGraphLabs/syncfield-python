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
import signal
import threading
import webbrowser
from contextlib import contextmanager
from typing import Iterator, Optional

import uvicorn

from syncfield.orchestrator import SessionOrchestrator
from syncfield.types import SessionState
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

    # SIGTERM semantics: Python's default SIGTERM handler terminates
    # the process via the C-level default, which does NOT run
    # ``finally`` blocks or ``atexit`` handlers. That's how ``kill
    # <pid>`` used to leave OAK boards booted-and-held even though
    # ``launch()`` has an apparently-safe teardown in ``finally``.
    # Install a handler that raises ``SystemExit`` instead — that
    # unwinds the stack normally, finally runs, ``app.close()``
    # releases every device, and the process exits cleanly. SIGKILL
    # still bypasses everything (by design), so the operator needs a
    # replug for that case — but every cooperative signal now does
    # the right thing.
    _prev_sigterm = signal.getsignal(signal.SIGTERM)

    def _on_sigterm(signum, frame):  # noqa: ARG001
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:
        # Signal handlers can only be installed from the main thread.
        # When launch() is called off the main thread (unusual, but
        # e.g. some test harnesses), skip the install — the caller
        # gave up on signal-driven cleanup by choosing that thread.
        _prev_sigterm = None

    try:
        app.setup()
        app.run()
    except (KeyboardInterrupt, SystemExit):
        # Both cooperative exits — let `finally` tear down devices.
        # SystemExit is re-raised after close() so the exit code
        # propagates; KeyboardInterrupt is swallowed to match the
        # previous CLI ergonomic (no traceback on Ctrl+C).
        pass
    finally:
        app.close()
        if _prev_sigterm is not None:
            try:
                signal.signal(signal.SIGTERM, _prev_sigterm)
            except (ValueError, TypeError):  # pragma: no cover
                pass


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

        url = f"http://{self._host}:{self._port}"
        print(f"\n  SyncField Viewer running at: {url}\n")
        logger.info("Viewer started at %s", url)

        # Open browser in a background thread after a short delay
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
        """Stop uvicorn, the poller, and release device handles.

        In blocking mode (``launch``), the viewer owns the session
        lifecycle — if the session is still holding devices when the
        viewer shuts down (Ctrl+C, SIGTERM, browser close, crash) it
        MUST call ``session.disconnect()`` / ``stop()`` on its way out
        or downstream adapters leak hardware handles. Leaving an OAK
        booted-and-held like that is the specific failure mode that
        forces a physical USB replug to recover, so we take two
        best-effort steps:

        1. If a recording is in progress, stop it so the MP4 is
           finalised rather than truncated mid-flight.
        2. If any stream is still connected, disconnect it so the
           device handles go back to the OS.

        All per-stream exceptions during teardown are swallowed — the
        invariant is "release as much hardware as we can before this
        process exits", not "raise the first error we encounter".
        ``disconnect()`` itself is idempotent from the caller's
        perspective — we only call it while the session is in a state
        that allows it.
        """
        # Signal uvicorn to shut down first so HTTP requests don't
        # race against teardown. Safe if uvicorn already stopped.
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True

        # Best-effort session teardown. Order matters: stop() first so
        # any in-flight recording finalises cleanly, then disconnect()
        # so device handles are released. A failed stop() must NOT
        # prevent disconnect() — that's what leaks handles.
        try:
            state = self._session.state
        except Exception:  # pragma: no cover — accessor corner cases
            state = None
        if state == SessionState.RECORDING:
            try:
                self._session.stop()
            except Exception as exc:  # pragma: no cover — best-effort
                logger.warning("viewer.close: session.stop() raised: %s", exc)
        # Re-read state — stop() moves us to STOPPED.
        try:
            state = self._session.state
        except Exception:  # pragma: no cover
            state = None
        if state in (SessionState.CONNECTED, SessionState.STOPPED):
            try:
                self._session.disconnect()
            except Exception as exc:  # pragma: no cover — best-effort
                logger.warning(
                    "viewer.close: session.disconnect() raised: %s", exc
                )

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
