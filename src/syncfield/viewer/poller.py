"""Background thread that polls a SessionOrchestrator into SessionSnapshots.

The poller runs in its own daemon thread at a configurable cadence (default
10 Hz). On each tick it reads the session's public state, rolls per-stream
stats forward, and publishes an immutable :class:`SessionSnapshot` under a
lock. The viewer's render loop calls :meth:`SessionPoller.get_snapshot` on
every frame to fetch the latest one.

Separate from the poll loop, the poller subscribes to each stream's
``on_sample`` and ``on_health`` callbacks so per-sample data (for IMU plots
and health timelines) lands in the stats buffer in real time rather than
being lost between polls. This matters because poll ticks at 10 Hz would
otherwise miss ~90% of samples on a 100 Hz IMU.
"""

from __future__ import annotations

import threading
import time as _time
from collections import deque
from pathlib import Path
from typing import Dict, Optional

from syncfield.health.types import Incident, IncidentSnapshot
from syncfield.orchestrator import SessionOrchestrator
from syncfield.stream import Stream
from syncfield.types import SampleEvent, SessionState

from syncfield.viewer.state import (
    SessionSnapshot,
    StreamSnapshot,
    StreamStatsBuffer,
)


class SessionPoller:
    """Polls a :class:`SessionOrchestrator` and produces snapshots.

    Thread model:

    - The poll loop runs in a daemon background thread owned by this object.
    - Sample and health callbacks run on whichever stream thread emits them.
    - :meth:`get_snapshot` is safe to call from any thread; it returns the
      latest published snapshot under a lock.

    Args:
        session: The orchestrator to observe.
        interval_s: How often to produce a new snapshot. Default ``0.1``
            (10 Hz) matches the cadence the viewer needs for smooth UI
            updates without burning CPU.
    """

    def __init__(
        self,
        session: SessionOrchestrator,
        interval_s: float = 0.1,
    ) -> None:
        self._session = session
        self._interval_s = interval_s

        self._stats: Dict[str, StreamStatsBuffer] = {}
        self._snapshot: Optional[SessionSnapshot] = None
        self._snapshot_lock = threading.Lock()

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._recording_started_at: Optional[float] = None
        self._last_observed_state: SessionState = SessionState.IDLE

        # Incident tracking — fed by HealthSystem callbacks.
        self._incidents_lock = threading.Lock()
        self._open_by_id: dict[str, Incident] = {}
        self._resolved: deque[Incident] = deque(maxlen=20)

        session.health.on_incident_opened = self._ingest_incident
        session.health.on_incident_updated = self._ingest_incident
        session.health.on_incident_closed = self._ingest_incident

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Subscribe to all streams' callbacks and begin polling."""
        self._register_callbacks()
        self._thread = threading.Thread(
            target=self._poll_loop, name="syncfield-viewer-poller", daemon=True
        )
        self._stop.clear()
        self._thread.start()

    def stop(self) -> None:
        """Signal the poll thread and join it."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def get_snapshot(self) -> Optional[SessionSnapshot]:
        """Return the latest snapshot, or ``None`` if the poller never ran."""
        with self._snapshot_lock:
            return self._snapshot

    # ------------------------------------------------------------------
    # Callback wiring
    # ------------------------------------------------------------------

    def _register_callbacks(self) -> None:
        """Attach on_sample to each registered stream.

        Re-entrant: callbacks for streams we've already registered for are
        skipped by tracking which stream ids already have a buffer.
        """
        for stream_id, stream in self._session._streams.items():  # type: ignore[attr-defined]
            if stream_id in self._stats:
                continue
            buffer = StreamStatsBuffer()
            self._stats[stream_id] = buffer
            stream.on_sample(self._make_sample_callback(stream_id, buffer))

    @staticmethod
    def _make_sample_callback(stream_id: str, buffer: StreamStatsBuffer):
        def _on_sample(event: SampleEvent) -> None:
            buffer.observe_sample(event.capture_ns, event.channels)

        return _on_sample

    def _ingest_incident(self, incident: Incident) -> None:
        """Called from the HealthSystem worker thread whenever an incident changes."""
        with self._incidents_lock:
            if incident.is_open:
                self._open_by_id[incident.id] = incident
            else:
                self._open_by_id.pop(incident.id, None)
                self._resolved.append(incident)

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Take a snapshot every ``interval_s`` seconds until stopped."""
        while not self._stop.is_set():
            # Streams may be added after start(); re-register to catch any
            # latecomers. Idempotent — already-registered streams are skipped.
            self._register_callbacks()
            try:
                snapshot = self._build_snapshot()
            except Exception:
                # A bad snapshot should never take down the viewer — keep
                # the previous one around.
                snapshot = None
            if snapshot is not None:
                with self._snapshot_lock:
                    self._snapshot = snapshot
            self._stop.wait(self._interval_s)

    def _build_snapshot(self) -> SessionSnapshot:
        """Read the current session state into an immutable SessionSnapshot."""
        session = self._session

        # Track the recording start time so we can compute elapsed seconds.
        current_state: SessionState = session.state
        now = _time.time()
        if (
            current_state is SessionState.RECORDING
            and self._last_observed_state is not SessionState.RECORDING
        ):
            self._recording_started_at = now
        elif current_state is not SessionState.RECORDING and current_state is not SessionState.STOPPING:
            self._recording_started_at = None
        self._last_observed_state = current_state

        elapsed_s = 0.0
        if self._recording_started_at is not None:
            elapsed_s = max(0.0, now - self._recording_started_at)

        now_ns = _time.monotonic_ns()
        streams_snapshot: Dict[str, StreamSnapshot] = {}
        for stream_id, stream in session._streams.items():  # type: ignore[attr-defined]
            buffer = self._stats.get(stream_id)
            if buffer is None:
                buffer = StreamStatsBuffer()
                self._stats[stream_id] = buffer

            plot_points = buffer.snapshot_plot() if stream.kind != "video" else {}
            latest_pose = buffer.snapshot_pose() if stream.kind != "video" else {}
            effective_hz = buffer.snapshot_fps(now_ns)
            latest_frame = self._safe_latest_frame(stream)

            # Prefer the adapter's own frame counter when available (video
            # adapters maintain `_frame_count` explicitly); otherwise fall
            # back to the poller's buffered sample count.
            if hasattr(stream, "_frame_count"):
                frame_count = int(getattr(stream, "_frame_count") or 0)
            else:
                frame_count = len(buffer._plot_timestamps)

            last_sample_at_ns: Optional[int] = (
                buffer._fps_window[-1] if buffer._fps_window else None
            )

            streams_snapshot[stream_id] = StreamSnapshot(
                id=stream_id,
                kind=stream.kind,
                provides_audio_track=stream.capabilities.provides_audio_track,
                produces_file=stream.capabilities.produces_file,
                frame_count=frame_count,
                last_sample_at_ns=last_sample_at_ns,
                effective_hz=effective_hz,
                latest_frame=latest_frame,
                plot_points=plot_points,
                latest_pose=latest_pose,
                live_preview=getattr(stream.capabilities, "live_preview", True),
            )

        # Snapshot incidents — take a consistent copy under the incidents lock.
        with self._incidents_lock:
            now_ns = _time.monotonic_ns()
            active = [
                IncidentSnapshot.from_incident(i, now_ns=now_ns)
                for i in self._open_by_id.values()
            ]
            resolved = [
                IncidentSnapshot.from_incident(i, now_ns=now_ns)
                for i in self._resolved
            ]

        # Session-level sync point + chirp fields.
        sync_point = getattr(session, "_sync_point", None)
        sp_mono = sync_point.monotonic_ns if sync_point is not None else None
        sp_wall = sync_point.wall_clock_ns if sync_point is not None else None
        chirp_start = getattr(session, "_chirp_start_ns", None)
        chirp_stop = getattr(session, "_chirp_stop_ns", None)
        chirp_enabled = bool(session._sync_tone.enabled)  # type: ignore[attr-defined]

        return SessionSnapshot(
            host_id=session.host_id,
            state=current_state.value,
            output_dir=str(Path(session.output_dir).resolve()),
            sync_point_monotonic_ns=sp_mono,
            sync_point_wall_clock_ns=sp_wall,
            chirp_start_ns=chirp_start,
            chirp_stop_ns=chirp_stop,
            chirp_enabled=chirp_enabled,
            elapsed_s=elapsed_s,
            streams=streams_snapshot,
            active_incidents=active,
            resolved_incidents=resolved,
        )

    @staticmethod
    def _safe_latest_frame(stream: Stream):
        """Read ``stream.latest_frame`` if the adapter exposes it."""
        frame = getattr(stream, "latest_frame", None)
        return frame
