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
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Dict, List, Optional, Union

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
    HealthEvent,
    SessionReport,
    SessionState,
    SyncPoint,
)
from syncfield.writer import SessionLogWriter, write_manifest, write_sync_point

logger = logging.getLogger(__name__)

#: Discriminated union of the multi-host role configs.
Role = Union[LeaderRole, FollowerRole]


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
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
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

            # Leader: start advertising in the PREPARING state so
            # followers already on the network see the session coming
            # up. Follower: block here until a leader advertises
            # `recording`. Both branches no-op for single-host.
            try:
                self._maybe_start_advertising()
                self._maybe_wait_for_leader()
            except Exception:
                self._stop_discovery_on_failure()
                self._transition(SessionState.IDLE)
                raise

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
                self._stop_discovery_on_failure()
                self._transition(SessionState.IDLE)
                raise

            self._maybe_play_start_chirp()
            self._transition(SessionState.RECORDING)

            # Leader only: flip the advertised status to `recording`
            # now that we actually are — the start chirp has played
            # and streams are live.
            self._maybe_update_advert_recording()

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

            # Leader: flip advert status to stopped BEFORE closing the
            # advertiser so every follower on the network observes the
            # transition. Close happens further down after artifacts
            # are persisted, which gives the graceful_shutdown_ms
            # margin time to propagate.
            self._maybe_update_advert_stopped()

            self._persist_session_artifacts(finalizations)

            self._transition(SessionState.STOPPED)
            if self._log_writer is not None:
                self._log_writer.close()
                self._log_writer = None

            # Tear down discovery. The advertiser's close() sleeps for
            # graceful_shutdown_ms before unregistering so followers
            # still browsing see the final "stopped" status; the
            # browser closes immediately because the follower has
            # already finalized its own streams.
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
    # Chirp injection
    # ------------------------------------------------------------------

    def _is_chirp_eligible(self) -> bool:
        """Return True if this host should play sync chirps.

        Chirp eligibility is a host-level check: chirps exist to
        enable inter-host audio cross-correlation, so they only matter
        if at least one registered stream actually captures audio.
        Single-host sessions with no audio stream happily rely on
        intra-host timestamp alignment and need no chirp at all.

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
        return any(
            s.capabilities.provides_audio_track for s in self._streams.values()
        )

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
            self._chirp_start = self._chirp_player.play(
                self._sync_tone.start_chirp
            )
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
