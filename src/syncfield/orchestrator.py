"""SessionOrchestrator — lifecycle coordinator for a multi-stream capture session.

The orchestrator owns state transitions, atomic start/stop across all
registered streams, chirp injection, crash-safe session logging, and
health-event routing. Each instance represents **one host**; multi-host
coordination happens at the sync core when outputs from multiple hosts
are submitted together.

The file is organized top-down so the public lifecycle is easy to read:

1. Construction and public properties
2. ``add()`` — stream registration
3. ``start()`` — atomic multi-stream start with rollback
4. ``stop()`` — chirp + finalization + artifact persistence
5. Session log helpers (crash safety)
6. Chirp injection helpers

Thread safety:
    ``add()`` is **not** thread-safe — call it from the thread that
    constructed the session. ``start()`` and ``stop()`` acquire an
    internal reentrant lock, so it is safe for other threads to observe
    state but only one lifecycle transition runs at a time.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from syncfield.clock import SessionClock
from syncfield.stream import Stream
from syncfield.tone import ChirpPlayer, SyncToneConfig, create_default_player
from syncfield.types import (
    FinalizationReport,
    HealthEvent,
    SessionReport,
    SessionState,
    SyncPoint,
)
from syncfield.writer import SessionLogWriter, write_manifest, write_sync_point

logger = logging.getLogger(__name__)


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
        chirp_player: ChirpPlayer | None = None,
    ) -> None:
        self._host_id = host_id
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._sync_tone = sync_tone or SyncToneConfig.default()
        self._chirp_player = chirp_player or create_default_player()
        self._streams: Dict[str, Stream] = {}
        self._state = SessionState.IDLE
        self._lock = threading.RLock()

        # Populated during start(); consumed during stop().
        self._sync_point: Optional[SyncPoint] = None
        self._session_clock: Optional[SessionClock] = None
        self._chirp_start_ns: Optional[int] = None
        self._chirp_stop_ns: Optional[int] = None
        self._log_writer: Optional[SessionLogWriter] = None

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
        rejected so session output files are always unique. Once
        ``start()`` has been called, any health events the stream emits
        are forwarded to the session log automatically.

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
        stream.on_health(self._on_stream_health)

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

            # Open the crash-safe session log BEFORE any state mutation so
            # failures during preparation are still recorded on disk.
            self._log_writer = SessionLogWriter(self._output_dir)
            self._log_writer.open()

            self._transition(SessionState.PREPARING)
            self._sync_point = SyncPoint.create_now(self._host_id)
            self._session_clock = SessionClock(sync_point=self._sync_point)

            started: List[Stream] = []
            try:
                for stream in self._streams.values():
                    stream.prepare()
                    stream.start(self._session_clock)
                    started.append(stream)
            except Exception as exc:
                self._log_rollback(exc, len(started))
                self._rollback_started_streams(started)
                self._transition(SessionState.IDLE)
                raise

            self._maybe_play_start_chirp()
            self._transition(SessionState.RECORDING)

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

    # ------------------------------------------------------------------
    # Lifecycle — stop
    # ------------------------------------------------------------------

    def stop(self) -> SessionReport:
        """Stop all streams and persist session artifacts.

        Sequence:
            1. Validate state (must be ``RECORDING``) and transition to
               ``STOPPING``.
            2. If chirp is eligible, play the stop chirp **before**
               stopping streams so it lands in recording audio tracks,
               then wait for its tail to flush.
            3. For each stream, call ``stop()``. Exceptions become failed
               :class:`FinalizationReport` entries — one slow or broken
               stream must never block finalization of the others.
            4. Write ``sync_point.json`` and ``manifest.json`` to the
               output directory.
            5. Transition to ``STOPPED``, close the session log, and
               return the aggregated :class:`SessionReport`.

        Returns:
            Aggregated :class:`SessionReport` with per-stream
            finalization reports and chirp timestamps (if a chirp was
            played).

        Raises:
            RuntimeError: If state is not ``RECORDING``.
        """
        with self._lock:
            if self._state is not SessionState.RECORDING:
                raise RuntimeError(
                    f"stop() requires RECORDING state; current state is {self._state.value}"
                )
            self._transition(SessionState.STOPPING)

            self._maybe_play_stop_chirp_and_wait()

            finalizations = self._finalize_streams()
            self._persist_session_artifacts(finalizations)

            self._transition(SessionState.STOPPED)
            if self._log_writer is not None:
                self._log_writer.close()
                self._log_writer = None
            return SessionReport(
                host_id=self._host_id,
                finalizations=finalizations,
                chirp_start_ns=self._chirp_start_ns,
                chirp_stop_ns=self._chirp_stop_ns,
            )

    def _finalize_streams(self) -> List[FinalizationReport]:
        """Call ``stop()`` on each stream and collect FinalizationReports.

        Stream exceptions are converted to failed reports so that one
        broken stream cannot prevent the session from reaching a clean
        ``STOPPED`` state. All finalize work for one stream happens
        before moving on to the next.
        """
        finalizations: List[FinalizationReport] = []
        for stream in self._streams.values():
            try:
                report = stream.stop()
            except Exception as exc:
                report = FinalizationReport(
                    stream_id=stream.id,
                    status="failed",
                    frame_count=0,
                    file_path=None,
                    first_sample_at_ns=None,
                    last_sample_at_ns=None,
                    health_events=[],
                    error=str(exc),
                )
            finalizations.append(report)
        return finalizations

    def _persist_session_artifacts(
        self,
        finalizations: List[FinalizationReport],
    ) -> None:
        """Write ``sync_point.json`` and ``manifest.json``.

        Assumes ``start()`` has already captured ``self._sync_point``;
        safe because ``stop()`` requires ``RECORDING`` state which can
        only be entered through ``start()``. Chirp fields are included
        only when a chirp was actually played — the writer omits
        ``chirp_*`` fields otherwise.
        """
        assert self._sync_point is not None  # guaranteed by state check

        chirp_spec = (
            self._sync_tone.start_chirp if self._chirp_start_ns is not None else None
        )
        write_sync_point(
            self._sync_point,
            self._output_dir,
            chirp_start_ns=self._chirp_start_ns,
            chirp_stop_ns=self._chirp_stop_ns,
            chirp_spec=chirp_spec,
        )

        streams_dict: Dict[str, dict] = {}
        final_by_id = {f.stream_id: f for f in finalizations}
        for stream in self._streams.values():
            entry: dict = {
                "kind": stream.kind,
                "capabilities": stream.capabilities.to_dict(),
            }
            final = final_by_id.get(stream.id)
            if final is not None:
                entry["status"] = final.status
                entry["frame_count"] = final.frame_count
                if final.file_path is not None:
                    entry["path"] = str(final.file_path)
                if final.error is not None:
                    entry["error"] = final.error
            streams_dict[stream.id] = entry

        write_manifest(self._host_id, streams_dict, self._output_dir)

    # ------------------------------------------------------------------
    # Session log helpers (crash safety)
    # ------------------------------------------------------------------

    def _transition(self, new_state: SessionState) -> None:
        """Record a state transition in the session log and update state.

        This is the single source of truth for state mutations after the
        session log has been opened. Every transition is flushed to disk
        immediately so a crash mid-recording still leaves an ordered
        timeline that the sync core can reconstruct.
        """
        old = self._state
        self._state = new_state
        if self._log_writer is not None:
            self._log_writer.log_event(
                {
                    "kind": "state_transition",
                    "from": old.value,
                    "to": new_state.value,
                    "at_ns": time.monotonic_ns(),
                }
            )

    def _log_rollback(self, exc: BaseException, started_count: int) -> None:
        """Persist a rollback event with the failing exception for post-mortem."""
        if self._log_writer is None:
            return
        self._log_writer.log_event(
            {
                "kind": "rollback",
                "reason": str(exc),
                "started_count": started_count,
                "at_ns": time.monotonic_ns(),
            }
        )

    def _on_stream_health(self, event: HealthEvent) -> None:
        """Forward a stream-reported health event into the session log.

        Events emitted before :meth:`start` (while the log is not yet
        open) are silently buffered by :class:`~syncfield.stream.StreamBase`
        and surface later in the :class:`FinalizationReport` so nothing
        is lost.
        """
        if self._log_writer is not None:
            self._log_writer.log_health(event)

    # ------------------------------------------------------------------
    # Chirp injection
    # ------------------------------------------------------------------

    def _is_chirp_eligible(self) -> bool:
        """Return True if this host should play sync chirps.

        Chirp eligibility is a host-level check: chirps exist to enable
        inter-host audio cross-correlation, so they only matter if at
        least one registered stream actually captures audio. Single-host
        sessions with no audio stream happily rely on intra-host
        timestamp alignment and need no chirp at all.
        """
        if not self._sync_tone.enabled:
            return False
        return any(
            s.capabilities.provides_audio_track for s in self._streams.values()
        )

    def _maybe_play_start_chirp(self) -> None:
        """Play the start chirp if eligible, else log an INFO line.

        Sleeps ``post_start_stabilization_ms`` first so audio capture
        pipelines have time to warm up and begin recording before the
        chirp hits the microphone.
        """
        if self._is_chirp_eligible():
            time.sleep(self._sync_tone.post_start_stabilization_ms / 1000.0)
            self._chirp_start_ns = time.monotonic_ns()
            self._chirp_player.play(self._sync_tone.start_chirp)
            return

        if self._sync_tone.enabled:
            logger.info(
                "[%s] No audio-capable stream registered on this host. "
                "Chirp injection disabled — host cannot participate in "
                "inter-host audio sync. Single-host sessions unaffected.",
                self._host_id,
            )

    def _maybe_play_stop_chirp_and_wait(self) -> None:
        """Play the stop chirp BEFORE stopping streams and wait for it to flush.

        The stop chirp must be captured in each recording audio track, so
        we play it first, then sleep for the chirp's duration plus a
        configurable tail margin, then let ``stop()`` proceed to finalize
        the streams.
        """
        if not self._is_chirp_eligible():
            return

        self._chirp_stop_ns = time.monotonic_ns()
        self._chirp_player.play(self._sync_tone.stop_chirp)
        total_wait_ms = (
            self._sync_tone.stop_chirp.duration_ms
            + self._sync_tone.pre_stop_tail_margin_ms
        )
        time.sleep(total_wait_ms / 1000.0)
