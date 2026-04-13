"""ControlPlaneServer — runs the FastAPI app under uvicorn on a worker thread.

One instance per :class:`~syncfield.orchestrator.SessionOrchestrator`
with a role. The server owns:

- a pre-bound TCP socket (see :mod:`_port_binding`),
- a uvicorn ``Server`` driven from a daemon thread, and
- an optional keep-alive timer that tears the server down a configurable
  delay after :meth:`arm_keep_alive_shutdown` is called. The timer can
  be preempted by :meth:`stop`.

The orchestrator decides *when* to arm the keep-alive timer. Typical
flow:

1. Orchestrator calls :meth:`start` inside ``start()``.
2. Session runs; routes read live state from ``app.state.orchestrator``.
3. Orchestrator's ``stop()`` calls :meth:`arm_keep_alive_shutdown`.
4. Either DELETE /session arrives first (routes call into the
   orchestrator which calls :meth:`stop`) or the timer fires and the
   server shuts itself down.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from syncfield.multihost.control_plane._port_binding import (
    DEFAULT_CONTROL_PLANE_PORT,
    bind_control_plane_port,
)
from syncfield.multihost.control_plane.routes import build_control_plane_app

logger = logging.getLogger(__name__)

#: Default post-stop keep-alive window (seconds). After the orchestrator's
#: ``stop()`` arms the timer, the server stays up this long so a leader
#: has time to pull files via :meth:`~syncfield.orchestrator.SessionOrchestrator.collect_from_followers`.
DEFAULT_KEEP_ALIVE_AFTER_STOP_SEC = 600.0


class ControlPlaneServer:
    """FastAPI + uvicorn wrapper with a thread-safe start/stop surface."""

    def __init__(
        self,
        orchestrator: Any,
        *,
        preferred_port: int = DEFAULT_CONTROL_PLANE_PORT,
        keep_alive_after_stop_sec: float = DEFAULT_KEEP_ALIVE_AFTER_STOP_SEC,
    ) -> None:
        self._orchestrator = orchestrator
        self._preferred_port = preferred_port
        self._keep_alive_after_stop_sec = keep_alive_after_stop_sec

        self._lock = threading.RLock()
        self._sock = None
        self._server: Optional[Any] = None  # uvicorn.Server
        self._thread: Optional[threading.Thread] = None
        self._actual_port: Optional[int] = None
        self._keep_alive_timer: Optional[threading.Timer] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def actual_port(self) -> int:
        """The port actually bound. Raises if :meth:`start` hasn't succeeded."""
        if self._actual_port is None:
            raise RuntimeError("ControlPlaneServer has not started")
        return self._actual_port

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Bind the socket and start uvicorn on a daemon thread.

        Blocks only long enough to bind the port; uvicorn's own startup
        runs on the worker thread. Use :func:`~httpx.get` on ``/health``
        with a retry loop to wait for readiness from the caller side.
        """
        import uvicorn  # lazy to keep the single-host path clean

        with self._lock:
            if self.is_running:
                raise RuntimeError("ControlPlaneServer already started")

            self._sock = bind_control_plane_port(preferred=self._preferred_port)
            self._actual_port = self._sock.getsockname()[1]

            app = build_control_plane_app(
                orchestrator=self._orchestrator,
                started_at_monotonic_s=time.monotonic(),
            )

            config = uvicorn.Config(
                app=app,
                fd=self._sock.fileno(),
                log_level="warning",
                # Disable uvicorn's own signal handling — we own the
                # lifecycle and signals would conflict with the host app.
                use_colors=False,
            )
            self._server = uvicorn.Server(config)
            # uvicorn normally installs signal handlers; skip that.
            self._server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

            self._thread = threading.Thread(
                target=self._server.run,
                name=f"control-plane-{self._actual_port}",
                daemon=True,
            )
            self._thread.start()

            logger.info(
                "ControlPlaneServer listening on 127.0.0.1:%d", self._actual_port
            )

    def stop(self, *, join_timeout_s: float = 5.0) -> None:
        """Tear the server down. Safe to call multiple times."""
        # Three separate lock acquisitions are deliberate: we release
        # before thread.join() so a callback running on the joined
        # thread (e.g. the keep-alive timer firing _keep_alive_expired
        # → stop() re-entry) can re-acquire without self-deadlocking.
        with self._lock:
            timer = self._keep_alive_timer
            self._keep_alive_timer = None
        if timer is not None:
            timer.cancel()

        with self._lock:
            server = self._server
            thread = self._thread
            sock = self._sock

        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=join_timeout_s)

        with self._lock:
            if sock is not None:
                try:
                    sock.close()
                except OSError:  # pragma: no cover - best-effort
                    pass
            self._sock = None
            self._server = None
            self._thread = None
            self._actual_port = None
            logger.info("ControlPlaneServer stopped")

    def arm_keep_alive_shutdown(self) -> None:
        """Schedule :meth:`stop` to run after ``keep_alive_after_stop_sec`` seconds.

        Safe to call multiple times; re-arming resets the timer. Cancelled
        by :meth:`stop`.
        """
        with self._lock:
            if self._keep_alive_timer is not None:
                self._keep_alive_timer.cancel()
            timer = threading.Timer(
                self._keep_alive_after_stop_sec, self._keep_alive_expired
            )
            timer.daemon = True
            self._keep_alive_timer = timer
            timer.start()
            logger.info(
                "ControlPlaneServer keep-alive armed: %.1fs",
                self._keep_alive_after_stop_sec,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _keep_alive_expired(self) -> None:
        logger.info("ControlPlaneServer keep-alive expired; stopping.")
        self.stop()
