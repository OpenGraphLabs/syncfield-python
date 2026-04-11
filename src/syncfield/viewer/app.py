"""Desktop viewer application — MuJoCo-style launcher for SyncField sessions.

This module owns the top-level DearPyGui context, the render loop, and the
small lifecycle machinery that makes :func:`launch` / :func:`launch_passive`
feel natural. The actual widget construction lives in
:mod:`syncfield.viewer.widgets` to keep this file focused on "how the app
runs" rather than "what each panel looks like".

Thread model:

- **Main thread** runs the DearPyGui render loop.
- **Poller thread** (daemon, started by :class:`SessionPoller`) populates
  :class:`SessionSnapshot`\\ s at 10 Hz.
- **Control worker thread** (daemon, started on button click) runs
  ``session.start()`` / ``session.stop()`` so the UI never blocks on SDK
  lifecycle calls.

DearPyGui itself is single-threaded for all UI mutation — the render loop
is the only thing that calls ``dpg.set_value`` / ``dpg.configure_item``.
Snapshots flow main thread via a single lock-guarded read.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

import dearpygui.dearpygui as dpg

from syncfield.orchestrator import SessionOrchestrator
from syncfield.viewer import theme
from syncfield.viewer.poller import SessionPoller
from syncfield.viewer.widgets.layout import ViewerLayout

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handle returned from launch_passive()
# ---------------------------------------------------------------------------


class ViewerHandle:
    """Minimal handle exposed by :func:`launch_passive`.

    Mirrors the shape of ``mujoco.viewer.launch_passive`` so callers coming
    from the MuJoCo / rerun worlds have zero friction.
    """

    def __init__(self, app: "ViewerApp") -> None:
        self._app = app

    def is_running(self) -> bool:
        """Return True while the viewer window is still open."""
        return self._app.is_running()

    def sync(self) -> None:
        """Render one frame. Use in passive mode when you own the loop."""
        self._app.render_one_frame()

    def close(self) -> None:
        """Close the viewer window and stop the poller."""
        self._app.close()


# ---------------------------------------------------------------------------
# Public launchers
# ---------------------------------------------------------------------------


def launch(
    session: SessionOrchestrator,
    *,
    title: str = "SyncField",
) -> None:
    """Open the viewer and block until the window is closed.

    In blocking mode the viewer *owns* the session lifecycle — the user
    clicks Record / Stop / Cancel in the UI, and the worker threads call
    the corresponding ``SessionOrchestrator`` methods. When the window
    closes, any in-progress recording is stopped cleanly.

    Args:
        session: The orchestrator to observe and control.
        title: Window title. Default ``"SyncField"``.
    """
    app = ViewerApp(session, title=title)
    try:
        app.setup()
        app.run()
    finally:
        app.close()


@contextmanager
def launch_passive(
    session: SessionOrchestrator,
    *,
    title: str = "SyncField",
) -> Iterator[ViewerHandle]:
    """Open the viewer in **passive** mode and return a handle.

    Use this when the caller owns the session lifecycle — e.g. a script
    that wants the GUI as an observer while it runs its own start/stop
    logic. The viewer's render loop runs on a background thread so the
    caller keeps control of the main thread.

    Example::

        with syncfield.viewer.launch_passive(session) as viewer:
            session.start()
            while viewer.is_running():
                time.sleep(0.1)
            session.stop()

    Note:
        Passive mode runs the DearPyGui render loop on a background
        thread. DPG is designed for a single UI thread and this works
        reliably on macOS and Linux in practice, but the blocking
        :func:`launch` path is the "MuJoCo-canonical" one if you don't
        need to share the main thread.
    """
    app = ViewerApp(session, title=title)
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
    """Owns the DearPyGui context and render loop for one viewer window.

    Separated from the module-level helpers so it can be instantiated
    directly in tests (or, in the future, embedded in a larger GUI).
    """

    def __init__(
        self,
        session: SessionOrchestrator,
        *,
        title: str = "SyncField",
        viewport_pos: Optional[tuple] = None,
    ) -> None:
        self._session = session
        self._title = title
        self._viewport_pos = viewport_pos
        self._poller = SessionPoller(session)
        self._layout: Optional[ViewerLayout] = None
        self._running = False
        self._setup_done = False
        self._close_requested = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Create the DPG context, build the layout, and start the poller."""
        if self._setup_done:
            return

        dpg.create_context()
        dpg.create_viewport(
            title=self._title,
            width=theme.VIEWPORT_WIDTH,
            height=theme.VIEWPORT_HEIGHT,
            small_icon="",
            large_icon="",
            resizable=True,
        )

        # Bind the global theme before any widgets are created so the
        # first frame doesn't flash with the default dark theme.
        global_theme_tag = theme.build_theme()
        dpg.bind_theme(global_theme_tag)

        # Viewport clear color matches the app background so the window
        # chrome edge doesn't leak through.
        dpg.set_viewport_clear_color(
            [c / 255 for c in theme.BG_APP]
        )

        self._layout = ViewerLayout(self._session)
        self._layout.build()

        dpg.setup_dearpygui()
        dpg.show_viewport()

        # Pin the viewport to a specific on-screen position when the caller
        # supplies one (used by the screenshot harness to place the window
        # at a known coordinate).
        if self._viewport_pos is not None:
            try:
                dpg.set_viewport_pos(self._viewport_pos)
            except Exception:
                pass

        # Make the primary window fill the viewport so resizing feels
        # native. The layout's main window is tagged "main_window".
        dpg.set_primary_window("main_window", True)

        self._poller.start()
        self._setup_done = True

    def run(self) -> None:
        """Run the render loop on the calling thread.

        Exits when the viewport is closed or :meth:`close` is called.
        """
        if not self._setup_done:
            self.setup()
        self._running = True
        try:
            while dpg.is_dearpygui_running() and not self._close_requested:
                self.render_one_frame()
        finally:
            self._running = False

    def render_one_frame(self) -> None:
        """Render a single DPG frame after syncing from the latest snapshot."""
        snapshot = self._poller.get_snapshot()
        if snapshot is not None and self._layout is not None:
            self._layout.update(snapshot)
        dpg.render_dearpygui_frame()

    def close(self) -> None:
        """Stop the poller and destroy the DPG context."""
        if self._close_requested:
            return
        self._close_requested = True
        self._poller.stop()
        try:
            if dpg.is_dearpygui_running():
                dpg.stop_dearpygui()
        except Exception:
            pass
        try:
            dpg.destroy_context()
        except Exception:
            pass
        self._setup_done = False

    def is_running(self) -> bool:
        return self._running and not self._close_requested

    # ------------------------------------------------------------------
    # Session control (called from widget callbacks)
    # ------------------------------------------------------------------

    def request_start(self) -> None:
        """Kick off ``session.start()`` on a worker thread so the UI stays live."""
        threading.Thread(
            target=self._safe_call,
            args=(self._session.start,),
            name="syncfield-viewer-start",
            daemon=True,
        ).start()

    def request_stop(self) -> None:
        """Kick off ``session.stop()`` on a worker thread."""
        threading.Thread(
            target=self._safe_call,
            args=(self._session.stop,),
            name="syncfield-viewer-stop",
            daemon=True,
        ).start()

    @staticmethod
    def _safe_call(fn) -> None:
        try:
            fn()
        except Exception:
            logger.exception("Viewer session control call failed")
