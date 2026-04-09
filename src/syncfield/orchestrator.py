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
from typing import Dict

from syncfield.stream import Stream
from syncfield.tone import SyncToneConfig
from syncfield.types import SessionState


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
