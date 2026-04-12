"""SessionOrchestrator — lifecycle coordinator for a multi-stream capture session.

The orchestrator owns state transitions, atomic start/stop across all
registered streams, chirp injection, crash-safe session logging, and
health-event routing. Each instance represents **one host**; multi-host
coordination happens at the sync core when outputs from multiple hosts
are submitted together.

Lifecycle
---------

SyncField 0.2 follows the same 4-phase lifecycle used by the egonaut
lab recorder::

    ┌─────────┐  connect()   ┌───────────┐  start()   ┌──────────┐
    │  IDLE   │─────────────▶│ CONNECTED │───────────▶│ COUNTDOWN│
    │         │◀─────────────│           │            └────┬─────┘
    └─────────┘ disconnect() └───────────┘                 │ 3/2/1
                                   ▲                       ▼
                                   │             ┌──────────────────┐
                             stop()│             │    RECORDING     │
                                   │             │ (streams writing)│
                                   │             └────────┬─────────┘
                                   │                      │ stop()
                                   │                      ▼
                                   │             ┌──────────────────┐
                                   │             │     STOPPING     │
                                   │             │ (chirp + finalize│
                                   │             └────────┬─────────┘
                                   └──────────────────────┘

* **Connect** opens device I/O on every stream so the viewer can
  render live preview data. No file is written.
* **Countdown** is a short visual 3/2/1 so the operator has a beat to
  glance at the rig before capture starts.
* **Start** atomically enables file writing on every stream, **then**
  plays the start chirp so the chirp lands inside the recorded audio.
* **Stop** plays the stop chirp **first** (so it also lands in audio),
  waits for the tail to flush, then tells every stream to stop writing.
  The devices stay connected — the operator can immediately start
  another recording without re-opening hardware.

Legacy compatibility
--------------------

Applications that used the 0.1 one-shot API (``session.start()`` →
``session.stop()``) continue to work. When ``start()`` is called from
``IDLE`` the orchestrator auto-connects, runs the countdown, starts
recording, and plays the chirp; ``stop()`` from that auto-connected
mode tears everything down and lands in ``STOPPED``.

Thread safety
-------------

``add()`` is **not** thread-safe — call it from the thread that
constructed the session. ``connect()`` / ``start()`` / ``stop()`` /
``disconnect()`` acquire an internal reentrant lock, so it is safe for
other threads to observe state but only one lifecycle transition runs
at a time.

The file is organized top-down so the public lifecycle is easy to read:

1. Construction and public properties
2. ``add()`` — stream registration
3. ``connect()`` — open device I/O for live preview
4. ``start()`` — countdown then atomic multi-stream record-start with rollback
5. ``stop()`` — chirp + finalization + return to CONNECTED
6. ``disconnect()`` — tear down device I/O
7. Session log helpers (crash safety)
8. Chirp injection helpers
"""

from __future__ import annotations

import logging
import threading
import time
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

from syncfield.clock import SessionClock
from syncfield.multihost.advertiser import SessionAdvertiser
from syncfield.multihost.browser import SessionBrowser
from syncfield.multihost.types import SessionAnnouncement
from syncfield.roles import FollowerRole, LeaderRole
from syncfield.stream import Stream
from syncfield.tone import ChirpPlayer, SyncToneConfig, create_default_player
from syncfield.types import (
    ChirpEmission,
    FinalizationReport,
    FrameTimestamp,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    SensorSample,
    SessionReport,
    SessionState,
    SyncPoint,
)
from syncfield.writer import (
    SensorWriter,
    SessionLogWriter,
    StreamWriter,
    write_manifest,
    write_sync_point,
)

#: Either of the per-stream sample-persistence writers. Video / audio /
#: custom streams get a :class:`StreamWriter` (``{id}.timestamps.jsonl``);
#: sensor streams get a :class:`SensorWriter` (``{id}.jsonl``). The
#: :class:`SessionOrchestrator` holds one writer per registered stream
#: for the duration of a recording cycle and closes them on stop.
SampleWriter = Union[StreamWriter, SensorWriter]

logger = logging.getLogger(__name__)

#: Discriminated union of the multi-host role configs.
Role = Union[LeaderRole, FollowerRole]


# ---------------------------------------------------------------------------
# Module-level helpers used by SessionOrchestrator.start() / stop() / connect()
# ---------------------------------------------------------------------------


def _generate_episode_path(data_dir: Path) -> Path:
    """Generate a timestamped episode path inside *data_dir*.

    Returns the path without creating the directory. The directory
    is only created when recording actually starts, so viewer-only
    sessions don't leave empty ``ep_*`` directories behind.
    """
    import secrets
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return data_dir / f"ep_{stamp}_{secrets.token_hex(3)}"


def _run_countdown(
    countdown_s: float,
    on_tick: Optional[Callable[[int], None]],
) -> None:
    """Block the calling thread for ``countdown_s`` seconds, ticking.

    Fires ``on_tick(n)`` once per remaining whole second in descending
    order (``3 → 2 → 1`` for ``countdown_s == 3``). The viewer uses
    this callback to render a big overlay countdown on the session
    clock panel. When ``countdown_s <= 0`` this is a no-op — useful
    for headless scripts that want atomic start semantics without the
    visual delay.
    """
    if countdown_s <= 0:
        return

    # Round up so non-integer durations still tick through every whole
    # second. A value of 2.5 ticks "3 → 2 → 1" and sleeps 2.5 s total.
    ticks = int(countdown_s)
    if ticks < 1:
        ticks = 1

    remaining = countdown_s
    for tick_value in range(ticks, 0, -1):
        if on_tick is not None:
            try:
                on_tick(tick_value)
            except Exception:  # pragma: no cover — callback must not break start()
                logger.exception("countdown tick callback raised")
        step = remaining / tick_value
        time.sleep(step)
        remaining -= step


def _rollback_disconnect_streams(connected: List["Stream"]) -> None:
    """Best-effort ``disconnect()`` on each stream, in LIFO order.

    Called during connect-rollback, stop-rollback, and the auto-connect
    stop path. Exceptions from individual streams are logged at DEBUG
    level and swallowed — tear-down must never leave a half-closed
    device in place.
    """
    for stream in reversed(connected):
        try:
            stream.disconnect()
        except Exception as exc:  # pragma: no cover — best-effort cleanup
            logger.debug("disconnect() raised for %s: %s", stream.id, exc)


def _rollback_stop_recording(recording: List["Stream"]) -> None:
    """Best-effort ``stop_recording()`` on each stream, in LIFO order.

    Called when ``start_recording()`` fails partway through the stream
    list. The streams that did manage to start are told to stop
    recording so the ones that succeeded don't keep writing after a
    rollback. Return values are discarded — a rollback is not a
    finalization.
    """
    for stream in reversed(recording):
        try:
            stream.stop_recording()
        except Exception as exc:  # pragma: no cover — best-effort cleanup
            logger.debug("stop_recording() raised for %s: %s", stream.id, exc)


class SessionOrchestrator:
    """Coordinates a multi-stream recording session for one host.

    A single orchestrator represents **one host**. Multi-host
    coordination happens via the optional ``role`` parameter, which
    plugs a :class:`~syncfield.multihost.SessionAdvertiser` (leader) or
    a :class:`~syncfield.multihost.SessionBrowser` (follower) into the
    lifecycle. Single-host callers omit ``role`` entirely and see no
    behavioral change.

    Args:
        host_id: Identifier for this capture host. Must match across
            all orchestrators belonging to the same logical host.
        output_dir: Directory where all output files are written.
            Created if it does not exist.
        sync_tone: Chirp configuration. Defaults to enabled with the
            egonaut production chirp spec. Use
            :meth:`~syncfield.tone.SyncToneConfig.silent` to disable.
        chirp_player: Optional custom player. Defaults to the
            best-available player via
            :func:`~syncfield.tone.create_default_player`.
        role: Optional multi-host role. Supply
            :class:`~syncfield.roles.LeaderRole` to advertise this
            session on the local network, or
            :class:`~syncfield.roles.FollowerRole` to block on
            :meth:`start` until a leader is advertising ``recording``.
            Followers **never** play chirps — they rely on the
            leader's chirps being captured by every host's microphones
            in the same physical space.
    """

    def __init__(
        self,
        host_id: str,
        output_dir: Path | str,
        sync_tone: SyncToneConfig | None = None,
        chirp_player: ChirpPlayer | None = None,
        role: Optional[Role] = None,
    ) -> None:
        self._host_id = host_id
        self._data_root = Path(output_dir)
        self._data_root.mkdir(parents=True, exist_ok=True)
        self._output_dir = _generate_episode_path(self._data_root)
        self._sync_tone = sync_tone or SyncToneConfig.default()
        self._chirp_player = chirp_player or create_default_player()
        self._streams: Dict[str, Stream] = {}
        self._state = SessionState.IDLE
        self._lock = threading.RLock()
        self._role: Optional[Role] = role

        # Multi-host infrastructure — populated only when role is set.
        self._advertiser: Optional[SessionAdvertiser] = None
        self._browser: Optional[SessionBrowser] = None
        self._observed_leader: Optional[SessionAnnouncement] = None

        # Populated during start(); consumed during stop().
        self._sync_point: Optional[SyncPoint] = None
        self._session_clock: Optional[SessionClock] = None
        self._chirp_start: Optional[ChirpEmission] = None
        self._chirp_stop: Optional[ChirpEmission] = None
        self._log_writer: Optional[SessionLogWriter] = None

        # Which streams successfully ``connect()``-ed so ``disconnect()``
        # on a partial failure only tears down the ones that actually
        # opened a device.
        self._connected_streams: List[Stream] = []

        # Auto-injected host audio stream (if any). Tracked so it can
        # be removed on disconnect.
        self._auto_audio_stream: Optional[Stream] = None

        # Flipped to True when the episode dir has been created on disk.
        self._episode_dir_created = False

        # Current task label — set by the viewer before recording.
        self._task: Optional[str] = None

        # True when the operator used the legacy one-shot ``start()`` from
        # ``IDLE`` instead of explicitly calling ``connect()`` first.
        # In that case ``stop()`` also tears down the devices and lands
        # the session in ``STOPPED`` for backward compatibility.
        self._auto_connected: bool = False

        # Sample persistence — one writer per registered stream, opened
        # at the start of every recording cycle and closed at its end.
        # ``_sample_handler_active`` holds a mutable flag per stream
        # whose sole purpose is to let ``_close_sample_writers`` flip
        # the corresponding handler closure into a no-op before the
        # underlying file handle is released, so any in-flight
        # ``SampleEvent`` from the capture thread can't race a
        # ``write()`` against a closed writer.
        self._sample_writers: Dict[str, SampleWriter] = {}
        self._sample_handler_active: Dict[str, List[bool]] = {}

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

    @property
    def task(self) -> Optional[str]:
        """Current task label for the next recording."""
        return self._task

    @task.setter
    def task(self, value: Optional[str]) -> None:
        self._task = value

    @property
    def role(self) -> Optional[Role]:
        """Return the attached multi-host role, or ``None`` for single-host."""
        return self._role

    @property
    def session_id(self) -> Optional[str]:
        """Return the shared multi-host session id.

        For :class:`LeaderRole` the id is known at construction time
        (auto-generated if the caller didn't supply one). For
        :class:`FollowerRole` the id may come from the role config
        or — when the follower uses auto-discovery — from the leader
        announcement observed during :meth:`start`. Returns ``None``
        for single-host sessions.
        """
        if isinstance(self._role, LeaderRole):
            return self._role.session_id
        if isinstance(self._role, FollowerRole):
            if self._role.session_id is not None:
                return self._role.session_id
            if self._observed_leader is not None:
                return self._observed_leader.session_id
        return None

    @property
    def observed_leader(self) -> Optional[SessionAnnouncement]:
        """Last announcement observed from the leader (follower-only)."""
        return self._observed_leader

    # ------------------------------------------------------------------
    # Stream registration
    # ------------------------------------------------------------------

    def add(self, stream: Stream) -> None:
        """Register a stream with this session.

        Must be called before :meth:`start`. Duplicate stream ids are
        rejected so session output files are always unique, **and**
        streams that point to the same physical device as one that's
        already registered (matched by ``stream.device_key``) are
        rejected too — this stops code + discovery-modal double-adds
        from creating two cards for the same webcam. Streams that
        return ``None`` from ``device_key`` (no hardware identity)
        are compared on stream-id only.

        Once ``start()`` has been called, any health events the stream
        emits are forwarded to the session log automatically.

        Raises:
            ValueError: If a stream with the same id is already
                registered, or another stream already owns the same
                physical device.
            RuntimeError: If the session is not in the ``IDLE`` state.
        """
        if self._state is not SessionState.IDLE:
            raise RuntimeError(
                f"add() requires IDLE state; current state is {self._state.value}"
            )
        if stream.id in self._streams:
            raise ValueError(f"duplicate stream id: {stream.id!r}")
        new_key = getattr(stream, "device_key", None)
        if new_key is not None:
            for existing in self._streams.values():
                existing_key = getattr(existing, "device_key", None)
                if existing_key == new_key:
                    raise ValueError(
                        f"physical device {new_key} is already registered "
                        f"as stream {existing.id!r}"
                    )
        self._streams[stream.id] = stream
        stream.on_health(self._on_stream_health)

        # After the first non-audio stream is registered, check whether
        # to pre-register a host audio stream so it appears in the
        # viewer immediately (before Connect/Record is pressed).
        if self._auto_audio_stream is None and not stream.capabilities.provides_audio_track:
            self._maybe_preregister_host_audio()

    def remove(self, stream_id: str) -> None:
        """Unregister a previously added stream.

        Valid in :attr:`SessionState.IDLE`, :attr:`CONNECTED`, and
        :attr:`STOPPED` states. Refuses during ``CONNECTING``,
        ``PREPARING``, ``COUNTDOWN``, ``RECORDING``, and ``STOPPING``
        because tearing a stream out of the session mid-lifecycle
        would leave partial artifacts on disk.

        If the session is currently ``CONNECTED``, the stream's
        device is disconnected first so its hardware handle is
        released before the stream leaves the registry.

        Args:
            stream_id: Id of the stream to remove.

        Raises:
            KeyError: If ``stream_id`` is not registered.
            RuntimeError: If the session is in a state that does not
                allow stream removal.
        """
        valid_states = (
            SessionState.IDLE,
            SessionState.CONNECTED,
            SessionState.STOPPED,
        )
        with self._lock:
            if self._state not in valid_states:
                raise RuntimeError(
                    "remove() requires one of "
                    f"{[s.value for s in valid_states]}; current state is "
                    f"{self._state.value}"
                )
            if stream_id not in self._streams:
                raise KeyError(f"unknown stream id: {stream_id!r}")

            stream = self._streams[stream_id]

            # If the session is connected (live preview running), tear
            # this stream's device down before unregistering so no
            # background thread keeps a dead reference to it.
            if self._state is SessionState.CONNECTED:
                try:
                    stream.disconnect()
                except Exception as exc:  # pragma: no cover — best-effort
                    logger.debug(
                        "disconnect() raised while removing %s: %s",
                        stream_id,
                        exc,
                    )
                try:
                    self._connected_streams.remove(stream)
                except ValueError:  # pragma: no cover — defensive
                    pass

            del self._streams[stream_id]
            logger.info("removed stream %s", stream_id)

    # ------------------------------------------------------------------
    # Lifecycle — connect
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open device I/O on every registered stream.

        Transitions ``IDLE → CONNECTING → CONNECTED``. Each stream's
        ``prepare()`` runs first (for permission checks and one-shot
        setup) and then ``connect()`` opens the underlying device and
        begins live capture for preview. After this call the viewer can
        render ``latest_frame`` / plot values without any file being
        written to disk.

        If any stream raises during ``prepare`` or ``connect``, every
        stream that successfully connected so far is disconnected in
        LIFO order and the exception re-raises. The session lands back
        in ``IDLE`` with no lingering device handles.

        Raises:
            RuntimeError: If the session is not in the ``IDLE`` or
                ``STOPPED`` state, or if no streams are registered.
            Exception: Any exception from a stream during prepare /
                connect propagates after rollback.
        """
        with self._lock:
            if self._state not in (SessionState.IDLE, SessionState.STOPPED):
                raise RuntimeError(
                    f"connect() requires IDLE or STOPPED state; current state is "
                    f"{self._state.value}"
                )
            if not self._streams:
                raise RuntimeError("cannot connect() with no streams registered")

            self._transition(SessionState.CONNECTING)

            connected: List[Stream] = []
            try:
                for stream in self._streams.values():
                    stream.prepare()
                    stream.connect()
                    connected.append(stream)
                    # Emit a health event so the viewer's Health Events
                    # panel confirms each device connected successfully.
                    stream._emit_health(HealthEvent(
                        stream_id=stream.id,
                        kind=HealthEventKind.HEARTBEAT,
                        at_ns=time.monotonic_ns(),
                        detail="connected",
                    ))
            except Exception as exc:
                self._log_rollback(exc, len(connected))
                _rollback_disconnect_streams(connected)
                self._transition(SessionState.IDLE)
                if self._log_writer is not None:
                    self._log_writer.close()
                    self._log_writer = None
                raise

            self._connected_streams = connected

            # Auto-inject host audio if no stream provides an audio track.
            # This enables multi-host cross-correlation sync without the
            # user having to add an audio stream manually.
            self._maybe_inject_host_audio()

            self._transition(SessionState.CONNECTED)

    # ------------------------------------------------------------------
    # Lifecycle — start (countdown → record → chirp)
    # ------------------------------------------------------------------

    def start(
        self,
        *,
        countdown_s: float = 3.0,
        on_countdown_tick: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Run the countdown, start recording, and play the start chirp.

        Sequence:
            1. Validate state. If the session is ``IDLE``, auto-call
               :meth:`connect` first so legacy callers that skip the
               explicit connect step still work.
            2. Transition to ``COUNTDOWN`` and fire the optional
               ``on_countdown_tick`` callback for each remaining second
               (``3 → 2 → 1``). The viewer uses this to render a big
               overlay countdown.
            3. Capture a fresh :class:`~syncfield.types.SyncPoint`.
            4. Call ``start_recording(session_clock)`` on every stream
               in registration order. This is meant to be fast —
               adapters should do any slow setup inside ``connect()``.
            5. If any stream raises, roll back by calling
               ``stop_recording()`` on the streams that did start, then
               return to ``CONNECTED`` and re-raise.
            6. Play the start chirp. The chirp is intentionally
               **after** every stream has enabled file writing so the
               audio track actually captures it.
            7. Transition to ``RECORDING``.

        Args:
            countdown_s: How long to count down before recording starts.
                Pass ``0`` to skip the countdown entirely (useful for
                headless scripts). Default ``3.0`` seconds.
            on_countdown_tick: Optional callback invoked once per
                remaining second with the current tick value. Useful
                for rendering a GUI overlay. Called on the calling
                thread — the orchestrator does not spin up a worker.

        Raises:
            RuntimeError: If the session is not in ``IDLE``,
                ``STOPPED``, or ``CONNECTED`` states.
            Exception: Any exception raised by a stream during
                ``start_recording`` propagates after rollback.
        """
        with self._lock:
            if self._state in (SessionState.IDLE, SessionState.STOPPED):
                # Legacy one-shot path — auto-connect then proceed.
                self._auto_connected = True
                self.connect()
            elif self._state is not SessionState.CONNECTED:
                raise RuntimeError(
                    f"start() requires CONNECTED state; current state is "
                    f"{self._state.value}"
                )
            else:
                self._auto_connected = False

            # Create the episode directory on first recording.
            # The path was generated in __init__ and shared with streams
            # via output_dir, so we must NOT regenerate it here.
            if not self._episode_dir_created:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                self._episode_dir_created = True
                logger.info("Episode dir created: %s", self._output_dir)

            # Open session log now that the directory exists.
            if self._log_writer is None:
                self._log_writer = SessionLogWriter(self._output_dir)
                self._log_writer.open()

            # Multi-host: advertise PREPARING / wait for leader. Moved
            # out of the legacy PREPARING branch because CONNECTED
            # already lets us know devices are live.
            self._transition(SessionState.PREPARING)
            try:
                self._maybe_start_advertising()
                self._maybe_wait_for_leader()
            except Exception:
                self._stop_discovery_on_failure()
                # Auto-connected sessions tear all the way back to IDLE
                # on multi-host failure; explicit-connect sessions stay
                # in CONNECTED so the caller can retry without
                # re-opening hardware.
                if self._auto_connected:
                    _rollback_disconnect_streams(self._connected_streams)
                    self._connected_streams = []
                    self._auto_connected = False
                    self._transition(SessionState.IDLE)
                    if self._log_writer is not None:
                        self._log_writer.close()
                        self._log_writer = None
                else:
                    self._transition(SessionState.CONNECTED)
                raise

            # --- Countdown -------------------------------------------
            # Wrap the caller's visual callback with the tick beep so a
            # single tick produces both the "3 → 2 → 1" overlay and a
            # short audible cue. The tick plays first (so the sound
            # front arrives at the same moment the number appears),
            # then the user callback fires.
            self._transition(SessionState.COUNTDOWN)

            def _tick_with_beep(n: int) -> None:
                self._maybe_play_countdown_tick()
                if on_countdown_tick is not None:
                    on_countdown_tick(n)

            _run_countdown(countdown_s, _tick_with_beep)

            # --- Atomic start_recording ------------------------------
            self._sync_point = SyncPoint.create_now(self._host_id)
            self._session_clock = SessionClock(sync_point=self._sync_point)

            # Open persistence writers BEFORE start_recording so the
            # very first ``SampleEvent`` each adapter emits after
            # flipping its ``_recording`` flag already has a handler
            # attached. The writers live on ``self`` so the matching
            # close path in ``_finalize_streams`` can flush them.
            self._open_sample_writers()

            recording: List[Stream] = []
            try:
                for stream in self._streams.values():
                    stream.start_recording(self._session_clock)
                    recording.append(stream)
            except Exception as exc:
                # Roll back the streams that did start writing.
                self._log_rollback(exc, len(recording))
                _rollback_stop_recording(recording)
                # Close any writers we already opened — same path
                # the happy stop() flow takes, so trailing samples
                # from rolled-back streams don't race a closed file.
                self._close_sample_writers()
                self._stop_discovery_on_failure()

                # If the user took the legacy one-shot path through
                # IDLE, tear down devices too and land in IDLE to
                # preserve 0.1 rollback semantics. Explicit connect
                # callers stay in CONNECTED so they can retry without
                # re-opening hardware.
                if self._auto_connected:
                    _rollback_disconnect_streams(self._connected_streams)
                    self._connected_streams = []
                    self._auto_connected = False
                    self._transition(SessionState.IDLE)
                    if self._log_writer is not None:
                        self._log_writer.close()
                        self._log_writer = None
                else:
                    self._transition(SessionState.CONNECTED)
                raise

            # --- Start chirp — AFTER every stream is writing --------
            # This is the critical ordering: the chirp must land inside
            # the recorded audio track, so we wait until every stream
            # has enabled file writing before playing it.
            self._maybe_play_start_chirp()
            self._transition(SessionState.RECORDING)

            # Leader only: flip the advertised status to `recording`
            # now that we actually are — the start chirp has played
            # and streams are live.
            self._maybe_update_advert_recording()

    # ------------------------------------------------------------------
    # Lifecycle — stop
    # ------------------------------------------------------------------

    def stop(self) -> SessionReport:
        """Play the stop chirp, finalize recording, and return to CONNECTED.

        Sequence:
            1. Validate state (must be ``RECORDING``) and transition to
               ``STOPPING``.
            2. Play the stop chirp **before** any stream is told to
               stop writing, so the chirp lands in every recorded audio
               track. Wait for the chirp tail to flush.
            3. Call ``stop_recording()`` on every stream. Exceptions
               become failed :class:`FinalizationReport` entries — one
               slow or broken stream must never block finalization of
               the others.
            4. Write ``sync_point.json`` and ``manifest.json`` to the
               output directory.
            5. Return the session to ``CONNECTED`` so the operator can
               start another recording immediately without re-opening
               hardware. Legacy one-shot callers (who reached
               ``RECORDING`` via an auto-connect from ``IDLE``) are
               taken all the way to ``STOPPED`` instead, matching the
               0.1 behavior.
            6. Return the aggregated :class:`SessionReport`.

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

            # --- Stop chirp — BEFORE any stream is told to stop -----
            # This is the critical ordering: the chirp must be captured
            # inside every recorded audio track, so we play it first
            # and let its tail flush before telling streams to stop.
            self._maybe_play_stop_chirp_and_wait()

            finalizations = self._finalize_streams()

            # Leader: flip advert status to stopped BEFORE closing the
            # advertiser so every follower on the network observes the
            # transition. Close happens further down after artifacts
            # are persisted, which gives the graceful_shutdown_ms
            # margin time to propagate.
            self._maybe_update_advert_stopped()

            self._persist_session_artifacts(finalizations)

            # --- Landing state -------------------------------------
            # If the caller explicitly connected before calling start,
            # keep devices open so they can record again. Otherwise
            # (legacy one-shot) tear down fully and land in STOPPED.
            if self._auto_connected:
                self._transition(SessionState.STOPPED)
                if self._log_writer is not None:
                    self._log_writer.close()
                    self._log_writer = None
                _rollback_disconnect_streams(self._connected_streams)
                self._connected_streams = []
                self._stop_discovery_on_failure()
                self._auto_connected = False
            else:
                self._transition(SessionState.CONNECTED)
                # Leave the session log open for the next recording in
                # this connected session. It gets flushed on each
                # transition.
                self._stop_discovery_on_failure()

            role_str = self._role.kind if self._role is not None else None
            return SessionReport(
                host_id=self._host_id,
                finalizations=finalizations,
                chirp_start_ns=(
                    self._chirp_start.best_ns
                    if self._chirp_start is not None
                    else None
                ),
                chirp_stop_ns=(
                    self._chirp_stop.best_ns
                    if self._chirp_stop is not None
                    else None
                ),
                chirp_start_source=(
                    self._chirp_start.source
                    if self._chirp_start is not None
                    else None
                ),
                chirp_stop_source=(
                    self._chirp_stop.source
                    if self._chirp_stop is not None
                    else None
                ),
                session_id=self.session_id,
                role=role_str,
            )

    def cancel(self) -> None:
        """Cancel recording and discard the episode.

        Stops all streams without playing a stop chirp, removes the
        episode directory entirely (including any partial files), and
        generates a fresh episode path for the next recording.

        Transitions ``RECORDING`` → ``CONNECTED`` (or ``STOPPED`` for
        legacy one-shot callers), same as :meth:`stop`.

        Raises:
            RuntimeError: If state is not ``RECORDING``.
        """
        import shutil

        with self._lock:
            if self._state is not SessionState.RECORDING:
                raise RuntimeError(
                    f"cancel() requires RECORDING state; current state is "
                    f"{self._state.value}"
                )
            self._transition(SessionState.STOPPING)

            # Stop all streams without chirp — just tear down
            for stream in self._connected_streams:
                try:
                    stream.stop_recording()
                except Exception:
                    logger.debug("Stream %s stop_recording failed during cancel", stream.id)

            self._close_sample_writers()

            # Close log writer BEFORE rmtree so no open file handles
            if self._log_writer is not None:
                self._log_writer.close()
                self._log_writer = None

            # Delete the episode directory and all contents
            if self._output_dir.exists():
                try:
                    shutil.rmtree(self._output_dir)
                    logger.info("Cancelled recording — deleted %s", self._output_dir)
                except Exception as exc:
                    logger.warning("Failed to delete episode dir: %s", exc)

            # Generate a new episode path for the next recording
            self._output_dir = _generate_episode_path(self._data_root)
            self._episode_dir_created = False

            if self._auto_connected:
                self._transition(SessionState.STOPPED)
            else:
                self._transition(SessionState.CONNECTED)

    # ------------------------------------------------------------------
    # Lifecycle — disconnect
    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        """Close device I/O on every connected stream.

        Transitions ``CONNECTED`` or ``STOPPED`` back to ``IDLE``. Each
        stream's ``disconnect()`` is called in reverse registration
        order so later-opened devices release their resources before
        earlier-opened ones. Exceptions from individual streams are
        logged and swallowed — tear-down must never leave a connected
        device behind.

        Raises:
            RuntimeError: If the session is in any state other than
                ``CONNECTED`` / ``STOPPED``.
        """
        with self._lock:
            if self._state not in (SessionState.CONNECTED, SessionState.STOPPED):
                raise RuntimeError(
                    f"disconnect() requires CONNECTED or STOPPED state; "
                    f"current state is {self._state.value}"
                )
            _rollback_disconnect_streams(self._connected_streams)
            self._connected_streams = []

            # Keep auto-injected audio stream registered (visible in viewer)
            # but disconnected. It will be reconnected on next connect().

            self._transition(SessionState.IDLE)
            if self._log_writer is not None:
                self._log_writer.close()
                self._log_writer = None

    def _finalize_streams(self) -> List[FinalizationReport]:
        """Call ``stop_recording()`` on each stream and collect FinalizationReports.

        Stream exceptions are converted to failed reports so that one
        broken stream cannot prevent the session from reaching a clean
        ``STOPPED`` state. All finalize work for one stream happens
        before moving on to the next.
        """
        finalizations: List[FinalizationReport] = []
        for stream in self._streams.values():
            try:
                report = stream.stop_recording()
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
        # Close persistence writers AFTER every adapter's capture loop
        # has observed ``_recording = False`` via ``stop_recording()``.
        # Doing it in this order means no sample writes race the
        # file-close path even on adapters that queue events between
        # the flag flip and the thread join.
        self._close_sample_writers()
        return finalizations

    # ------------------------------------------------------------------
    # Sample persistence — one writer per stream per recording cycle
    # ------------------------------------------------------------------

    def _open_sample_writers(self) -> None:
        """Create + wire a persistence writer for every registered stream.

        Called inside :meth:`start` just before the atomic
        ``start_recording()`` loop so the very first ``SampleEvent``
        each adapter emits under its ``_recording`` flag is already
        captured to disk. Two writer shapes, one per stream kind:

        * ``stream.kind == "sensor"`` → :class:`SensorWriter`
          producing ``{stream_id}.jsonl`` with channel values.
        * Everything else (video / audio / custom) →
          :class:`StreamWriter` producing
          ``{stream_id}.timestamps.jsonl`` with frame timestamps only.

        The handler closure holds a mutable ``active`` flag that
        :meth:`_close_sample_writers` flips to ``False`` before
        releasing the file handle, so trailing samples from the
        capture thread become no-ops instead of writing to a closed
        writer.
        """
        for stream in self._streams.values():
            writer: SampleWriter
            if stream.kind == "sensor":
                writer = SensorWriter(stream.id, self._output_dir)
            else:
                writer = StreamWriter(stream.id, self._output_dir)
            writer.open()
            active: List[bool] = [True]
            stream.on_sample(self._make_sample_handler(writer, active))
            self._sample_writers[stream.id] = writer
            self._sample_handler_active[stream.id] = active

    def _make_sample_handler(
        self,
        writer: SampleWriter,
        active: List[bool],
    ) -> Callable[[SampleEvent], None]:
        """Build the ``on_sample`` callback that persists events.

        Separated from :meth:`_open_sample_writers` so the closure
        captures exactly ``writer`` + ``active`` + ``host_id`` and
        nothing else — no stray references to the orchestrator that
        would keep it alive across sessions.
        """
        host_id = self._host_id

        def _handle(event: SampleEvent) -> None:
            if not active[0]:
                return
            try:
                if isinstance(writer, SensorWriter):
                    writer.write(
                        SensorSample(
                            frame_number=event.frame_number,
                            capture_ns=event.capture_ns,
                            channels=event.channels or {},
                            clock_source="host_monotonic",
                            clock_domain=host_id,
                            uncertainty_ns=event.uncertainty_ns,
                        )
                    )
                else:
                    writer.write(
                        FrameTimestamp(
                            frame_number=event.frame_number,
                            capture_ns=event.capture_ns,
                            clock_source="host_monotonic",
                            clock_domain=host_id,
                            uncertainty_ns=event.uncertainty_ns,
                        )
                    )
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning(
                    "sample writer for %s raised %s: %s",
                    event.stream_id,
                    type(exc).__name__,
                    exc,
                )

        return _handle

    def _close_sample_writers(self) -> None:
        """Flush and close every per-stream sample writer.

        Flips every handler's ``active`` flag to ``False`` FIRST so
        any trailing ``SampleEvent`` already in flight from the
        capture thread becomes a no-op before we close the backing
        file handles. Swallows per-writer close errors so a single
        broken file cannot block the rest of the teardown.
        """
        for active in self._sample_handler_active.values():
            active[0] = False
        for stream_id, writer in self._sample_writers.items():
            try:
                writer.close()
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning(
                    "closing sample writer for %s raised %s: %s",
                    stream_id,
                    type(exc).__name__,
                    exc,
                )
        self._sample_writers.clear()
        self._sample_handler_active.clear()

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

        Both the best-available timestamp (``chirp_*_ns``) and the
        provenance tag (``chirp_*_source``) are threaded through so
        the downstream sync core can decide whether to claim sub-ms
        (``hardware``) or ~1 ms (``software_fallback``) precision for
        this host. Multi-host ``session_id`` / ``role`` /
        ``leader_host_id`` are written for both leader and follower
        so the sync core can reconstruct the host relationship.
        """
        assert self._sync_point is not None  # guaranteed by state check

        role_str = self._role.kind if self._role is not None else None
        leader_host_id: Optional[str] = None
        if isinstance(self._role, FollowerRole) and self._observed_leader is not None:
            leader_host_id = self._observed_leader.host_id

        chirp_spec = (
            self._sync_tone.start_chirp if self._chirp_start is not None else None
        )
        write_sync_point(
            self._sync_point,
            self._output_dir,
            chirp_start_ns=(
                self._chirp_start.best_ns if self._chirp_start is not None else None
            ),
            chirp_stop_ns=(
                self._chirp_stop.best_ns if self._chirp_stop is not None else None
            ),
            chirp_start_source=(
                self._chirp_start.source if self._chirp_start is not None else None
            ),
            chirp_stop_source=(
                self._chirp_stop.source if self._chirp_stop is not None else None
            ),
            chirp_spec=chirp_spec,
            session_id=self.session_id,
            role=role_str,
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

        write_manifest(
            self._host_id,
            streams_dict,
            self._output_dir,
            session_id=self.session_id,
            role=role_str,
            leader_host_id=leader_host_id,
            task=self._task,
        )

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
    # Host audio auto-injection
    # ------------------------------------------------------------------

    def _maybe_preregister_host_audio(self) -> None:
        """Pre-register a :class:`HostAudioStream` so it shows in the viewer.

        Called from :meth:`add` after the first non-audio stream is
        registered. Only registers the stream (no device open) so the
        viewer can display the audio card immediately. The actual device
        connection happens in :meth:`connect` along with all other streams.
        """
        try:
            from syncfield.adapters.host_audio import (
                HostAudioStream,
                is_audio_available,
            )
        except ImportError:
            return

        if not is_audio_available():
            return

        try:
            audio = HostAudioStream("host_audio", output_dir=self._output_dir)
            self._streams[audio.id] = audio
            audio.on_health(self._on_stream_health)
            self._auto_audio_stream = audio
            logger.info("Pre-registered host audio stream (mic detected)")
        except Exception as exc:
            logger.debug("Failed to pre-register host audio: %s", exc)

    def _maybe_inject_host_audio(self) -> None:
        """Ensure the auto audio stream is connected during connect().

        If ``_maybe_preregister_host_audio`` already added the stream,
        this is a no-op (connect loop handles it). If not yet added
        (e.g. user skipped add() and went straight to connect()), this
        adds and connects it now.
        """
        has_audio = any(
            s.capabilities.provides_audio_track
            for s in self._streams.values()
        )
        if has_audio:
            return

        # Already pre-registered? connect() loop will handle it.
        if self._auto_audio_stream is not None:
            return

        try:
            from syncfield.adapters.host_audio import (
                HostAudioStream,
                is_audio_available,
            )
        except ImportError:
            return

        if not is_audio_available():
            return

        try:
            audio = HostAudioStream("host_audio", output_dir=self._output_dir)
            audio.prepare()
            audio.connect()
            self._streams[audio.id] = audio
            self._connected_streams.append(audio)
            self._auto_audio_stream = audio
            logger.info("Auto-injected host audio stream (mic detected)")
        except Exception as exc:
            logger.warning("Failed to auto-inject host audio: %s", exc)

    # ------------------------------------------------------------------
    # Chirp injection
    # ------------------------------------------------------------------

    def _is_chirp_eligible(self) -> bool:
        """Return True if this host should play sync chirps.

        Chirps now serve two roles:

        1. **Operator feedback** — audible start/stop cues so whoever
           is driving a recording knows the session actually began
           and ended. This matters in every config, even a video-only
           single-host rig with no microphone attached.
        2. **Inter-host audio cross-correlation** — when at least one
           stream captures audio, the chirp also lands inside that
           track and becomes the shared acoustic anchor the sync
           service uses to align peer hosts. That's a side effect of
           playing the chirp, not a precondition for playing it.

        Up through 0.2.x the eligibility check also required at least
        one ``provides_audio_track=True`` stream, which meant a plain
        webcam rig recorded in total silence — operators pressed
        Record and heard nothing. The gate is gone: chirps now play
        whenever :class:`SyncToneConfig` is enabled and this host is
        not a follower. Silent operation is still available via
        :meth:`SyncToneConfig.silent`.

        **Followers never play chirps.** They rely on the leader's
        chirps being captured by every host's microphones in the same
        physical space — if every follower also played its own chirps
        they would interfere with each other and corrupt the shared
        acoustic anchors.
        """
        if isinstance(self._role, FollowerRole):
            return False
        if not self._sync_tone.enabled:
            return False
        return True

    def _maybe_play_start_chirp(self) -> None:
        """Play the start chirp if eligible, else log an INFO line.

        Sleeps ``post_start_stabilization_ms`` first so audio capture
        pipelines have time to warm up and begin recording before the
        chirp hits the microphone. Stores the returned
        :class:`ChirpEmission` so both hardware and software timestamps
        are preserved for the session artifacts.
        """
        if self._is_chirp_eligible():
            time.sleep(self._sync_tone.post_start_stabilization_ms / 1000.0)
            try:
                self._chirp_start = self._chirp_player.play(
                    self._sync_tone.start_chirp
                )
            except Exception:  # pragma: no cover — audio path is best-effort
                logger.exception("start chirp playback failed")
            return

        if self._sync_tone.enabled:
            logger.info(
                "[%s] Chirp injection skipped (sync_tone.enabled=False or "
                "follower role). No operator start cue will be played.",
                self._host_id,
            )

    def _maybe_play_countdown_tick(self) -> None:
        """Play the configured countdown tick beep if eligible.

        Called from the countdown loop once per remaining second.
        Short (default 100 ms) and non-blocking — the
        :class:`SoundDeviceChirpPlayer` returns as soon as the audio
        backend's first callback fires, so the countdown sleep
        proceeds without waiting for the full tick to drain.
        Exceptions are swallowed: a misbehaving audio path should
        never prevent the recording from starting.
        """
        if not self._is_chirp_eligible():
            return
        tick = self._sync_tone.countdown_tick
        if tick is None:
            return
        try:
            self._chirp_player.play(tick)
        except Exception:  # pragma: no cover — audio path is best-effort
            logger.exception("countdown tick playback failed")

    def _maybe_play_stop_chirp_and_wait(self) -> None:
        """Play the stop chirp BEFORE stopping streams and wait for it to flush.

        The stop chirp must be captured in each recording audio track,
        so we play it first, then sleep for the chirp's duration plus a
        configurable tail margin, then let ``stop()`` proceed to
        finalize the streams. Stores the returned
        :class:`ChirpEmission` for the session artifacts.
        """
        if not self._is_chirp_eligible():
            return

        self._chirp_stop = self._chirp_player.play(self._sync_tone.stop_chirp)
        total_wait_ms = (
            self._sync_tone.stop_chirp.duration_ms
            + self._sync_tone.pre_stop_tail_margin_ms
        )
        time.sleep(total_wait_ms / 1000.0)

    # ------------------------------------------------------------------
    # Multi-host discovery (leader advertising + follower browsing)
    # ------------------------------------------------------------------

    def _maybe_start_advertising(self) -> None:
        """Leader-only: open an advertiser in the ``preparing`` state.

        No-op for follower and single-host sessions. Called from
        :meth:`start` inside the ``PREPARING`` transition so followers
        already on the network can see the session coming up before
        streams actually begin recording.
        """
        if not isinstance(self._role, LeaderRole):
            return
        assert self._role.session_id is not None  # post_init guarantees
        self._advertiser = SessionAdvertiser(
            session_id=self._role.session_id,
            host_id=self._host_id,
            sdk_version=_pkg_version("syncfield"),
            chirp_enabled=self._sync_tone.enabled,
            graceful_shutdown_ms=self._role.graceful_shutdown_ms,
        )
        self._advertiser.start()

    def _maybe_update_advert_recording(self) -> None:
        """Leader-only: flip the advert status to ``recording``.

        Called after streams have started and the start chirp has
        played so followers observing the advertiser see
        ``recording`` only when this host is actually ready. The
        embedded ``started_at_ns`` is the leader's own monotonic
        anchor — it lives in the leader's clock domain and must not
        be compared directly to a follower's clock.
        """
        if self._advertiser is None:
            return
        started_ns = self._sync_point.monotonic_ns if self._sync_point else None
        self._advertiser.update_status("recording", started_at_ns=started_ns)

    def _maybe_update_advert_stopped(self) -> None:
        """Leader-only: flip the advert status to ``stopped``.

        Called inside :meth:`stop` between the stop chirp and the
        teardown of the advertiser instance, so followers watching
        the TXT record observe the ``stopped`` transition before the
        service unregisters (via the advertiser's graceful shutdown
        sleep).
        """
        if self._advertiser is None:
            return
        self._advertiser.update_status("stopped")

    def _maybe_wait_for_leader(self) -> None:
        """Follower-only: block until a leader is advertising recording.

        Opens a :class:`SessionBrowser`, waits up to
        ``leader_wait_timeout_sec``, and stores the observed
        announcement on :attr:`_observed_leader`. No-op for leader
        and single-host sessions.

        Raises:
            TimeoutError: If no leader reaches ``recording`` before
                the deadline. Caller (``start()``) is responsible
                for cleaning up discovery state.
        """
        if not isinstance(self._role, FollowerRole):
            return
        self._browser = SessionBrowser(session_id=self._role.session_id)
        self._browser.start()
        self._observed_leader = self._browser.wait_for_recording(
            timeout=self._role.leader_wait_timeout_sec
        )

    def _stop_discovery_on_failure(self) -> None:
        """Tear down advertiser and browser, swallowing cleanup errors.

        Shared between the happy-path end of :meth:`stop` and the
        failure paths in :meth:`start` (rollback after stream start
        exception or follower wait timeout). Leaves
        :attr:`_advertiser` and :attr:`_browser` set to ``None`` so
        a subsequent session on the same orchestrator starts clean.
        """
        if self._advertiser is not None:
            try:
                self._advertiser.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._advertiser = None
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._browser = None

    def wait_for_leader_stopped(
        self, timeout: float = 3600.0
    ) -> SessionAnnouncement:
        """Block until the observed leader advertises ``status="stopped"``.

        Follower-only convenience so the caller can drive its own
        :meth:`stop` call off the leader's lifecycle instead of
        relying on a wall-clock deadline::

            session = SessionOrchestrator(
                host_id="follower", output_dir="./data",
                role=FollowerRole(session_id="amber-tiger-042"),
            )
            session.add(camera)
            session.start()              # blocks until leader recording
            session.wait_for_leader_stopped()
            session.stop()

        Args:
            timeout: Maximum seconds to wait. Default one hour.

        Raises:
            RuntimeError: If called on a non-follower orchestrator or
                before :meth:`start`.
            TimeoutError: If *timeout* elapses before the leader
                announces ``stopped``.
        """
        if not isinstance(self._role, FollowerRole):
            raise RuntimeError("wait_for_leader_stopped() requires FollowerRole")
        if self._browser is None:
            raise RuntimeError(
                "wait_for_leader_stopped() requires an active SessionBrowser; "
                "call start() first"
            )
        return self._browser.wait_for_stopped(timeout=timeout)
