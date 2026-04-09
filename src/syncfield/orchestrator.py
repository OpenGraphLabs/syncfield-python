"""SessionOrchestrator — lifecycle coordinator for a multi-stream capture session.

The orchestrator owns state transitions, atomic start/stop across all
registered streams, chirp injection, crash-safe manifest flushing, and
health-event routing. Each instance represents **one host**; multi-host
coordination happens at the sync core when outputs from multiple hosts are
submitted together.

This skeleton is expanded incrementally — the ``start()``/``stop()`` logic,
chirp integration, crash-safe session log, and health routing are added in
subsequent tasks.

Thread safety:
    ``add()`` is **not** thread-safe — call it from the thread that
    constructed the session. ``start()`` and ``stop()`` acquire an internal
    reentrant lock, so it is safe for other threads to observe state but
    only one lifecycle transition runs at a time.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List

from syncfield.clock import SessionClock
from syncfield.stream import Stream
from syncfield.tone import SyncToneConfig
from syncfield.types import SessionState, SyncPoint


class SessionOrchestrator:
    """Coordinates a multi-stream recording session for one host.

    Args:
        host_id: Identifier for this capture host. Must match across all
            orchestrators belonging to the same logical host.
        output_dir: Directory where all output files are written. Created
            if it does not exist.
        sync_tone: Chirp configuration. Defaults to enabled with the
            egonaut production chirp spec. Use
            :meth:`~syncfield.tone.SyncToneConfig.silent` to disable.
    """

    def __init__(
        self,
        host_id: str,
        output_dir: Path | str,
        sync_tone: SyncToneConfig | None = None,
    ) -> None:
        self._host_id = host_id
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._sync_tone = sync_tone or SyncToneConfig.default()
        self._streams: Dict[str, Stream] = {}
        self._state = SessionState.IDLE
        self._lock = threading.RLock()

        # Populated during start(); consumed during stop().
        self._sync_point: SyncPoint | None = None
        self._session_clock: SessionClock | None = None

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def host_id(self) -> str:
        return self._host_id

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    # ------------------------------------------------------------------
    # Stream registration
    # ------------------------------------------------------------------

    def add(self, stream: Stream) -> None:
        """Register a stream with this session.

        Must be called before :meth:`start`. Duplicate stream ids are
        rejected so session output files are always unique.

        Raises:
            ValueError: If a stream with the same id is already registered.
            RuntimeError: If the session is not in the ``IDLE`` state.
        """
        if self._state is not SessionState.IDLE:
            raise RuntimeError(
                f"add() requires IDLE state; current state is {self._state.value}"
            )
        if stream.id in self._streams:
            raise ValueError(f"duplicate stream id: {stream.id!r}")
        self._streams[stream.id] = stream

    # ------------------------------------------------------------------
    # Lifecycle — start
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start every registered stream atomically.

        Sequence:
            1. Validate state (must be ``IDLE``) and that at least one
               stream is registered.
            2. Capture a fresh :class:`~syncfield.types.SyncPoint` and
               build the shared :class:`~syncfield.clock.SessionClock`.
            3. For each stream: call ``prepare()`` then
               ``start(session_clock)``. If any call raises, roll back all
               streams that were fully started (stopping them in reverse
               order) and re-raise the original exception.
            4. On success, transition to ``RECORDING``.

        The failed stream itself is **not** rolled back — it never reached
        a successfully-started state.

        Raises:
            RuntimeError: If state is not ``IDLE`` or no streams are
                registered.
            Exception: Any exception raised by a stream during
                ``prepare``/``start`` propagates after rollback. State
                returns to ``IDLE`` before the exception escapes.
        """
        with self._lock:
            if self._state is not SessionState.IDLE:
                raise RuntimeError(
                    f"start() requires IDLE state; current state is {self._state.value}"
                )
            if not self._streams:
                raise RuntimeError("cannot start() with no streams registered")

            self._state = SessionState.PREPARING
            self._sync_point = SyncPoint.create_now(self._host_id)
            self._session_clock = SessionClock(sync_point=self._sync_point)

            started: List[Stream] = []
            try:
                for stream in self._streams.values():
                    stream.prepare()
                    stream.start(self._session_clock)
                    started.append(stream)
            except Exception:
                self._rollback_started_streams(started)
                self._state = SessionState.IDLE
                raise

            self._state = SessionState.RECORDING

    @staticmethod
    def _rollback_started_streams(started: List[Stream]) -> None:
        """Best-effort tear-down of streams that were fully started.

        Called when ``start()`` fails partway through. Streams are stopped
        in reverse order (LIFO) so later-started streams release their
        resources before earlier-started ones. Any exceptions raised by
        ``stop()`` during rollback are swallowed — the primary failure is
        already on its way up the stack and is the real story.
        """
        for s in reversed(started):
            try:
                s.stop()
            except Exception:  # pragma: no cover — best-effort cleanup
                pass
