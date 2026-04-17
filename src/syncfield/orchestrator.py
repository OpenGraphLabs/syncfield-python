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
from typing import Any, Callable, Dict, List, Optional, Union

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


def _generate_multihost_episode_path(
    data_dir: Path, *, session_id: str, host_id: str
) -> Path:
    """Generate a timestamped episode path nested under session + host.

    Mirrors :func:`_generate_episode_path` but prefixes the episode
    directory with ``<session_id>/<host_id>/`` so multi-host sessions
    from several machines can share the same ``data_dir`` without
    filename collisions. The directory is not created on disk — the
    caller is responsible for ``mkdir`` at the moment recording starts,
    identical to the single-host path helper.
    """
    return _generate_episode_path(data_dir / session_id / host_id)


#: Placeholder ``session_id`` embedded in the output path when an
#: auto-discover :class:`~syncfield.roles.FollowerRole` has not yet
#: observed its leader. Rewritten in-place by
#: :meth:`SessionOrchestrator._rewrite_output_dir_for_observed_session`
#: once the real id is known, before any files land on disk. Defined
#: as a module-level constant so the rewrite logic (Task 3) can compare
#: against a single symbol instead of string-matching this literal in
#: two places.
_PENDING_SESSION_PLACEHOLDER = "_pending_session"


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


def _sha256_of_local(path: Path) -> str:
    """Compute the sha256 hex digest of a local file in 64 KiB chunks.

    Used by :meth:`SessionOrchestrator.collect_from_followers` to verify
    that each downloaded file matches the digest reported by the
    follower's ``/files/manifest`` response. Streamed read so we don't
    hold large media files entirely in memory.
    """
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


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
        self._role: Optional[Role] = role

        # Multi-host infrastructure — populated only when a role is set.
        # Any attribute read by _compute_initial_output_dir() or its
        # helper _resolve_session_id_for_path (currently _observed_leader,
        # via the auto-discover fallback) MUST be initialized above the
        # self._output_dir = self._compute_initial_output_dir() line
        # below. Moving that call without checking this invariant will
        # raise AttributeError at __init__ time.
        self._advertiser: Optional[SessionAdvertiser] = None
        self._browser: Optional[SessionBrowser] = None
        self._observed_leader: Optional[SessionAnnouncement] = None
        # Signals set by the follower's /session/config route handler
        # the moment the leader's distribute POST lands. Leader only
        # POSTs after transitioning to RECORDING, so this is a
        # ground-truth cross-host wake-up signal that does not depend
        # on mDNS TXT propagation or on HTTP polling of the leader's
        # /health endpoint (both of which can be flaky on WiFi). Paired
        # with the HTTP poll in :meth:`_wait_for_leader_recording_state`
        # as a belt-and-braces mechanism.
        self._leader_recording_signal: threading.Event = threading.Event()
        # Background thread a follower uses to block on
        # ``SessionBrowser.wait_for_observation`` without stalling
        # :meth:`connect`. Populated by
        # :meth:`_maybe_start_follower_browser_in_background` and
        # cleared by :meth:`disconnect`.
        self._follower_observer_thread: Optional[threading.Thread] = None
        # Operator-injected peer list that bypasses mDNS discovery.
        # Populated via :meth:`set_static_peers`; consumed by
        # :meth:`_discover_followers_in_preparing` and
        # :meth:`collect_from_followers`. Empty list means "use mDNS"
        # (the normal LAN path). See set_static_peers() docstring for
        # why this escape hatch exists (macOS loopback).
        self._static_peers: list = []
        # Operator-injected leader announcement that bypasses mDNS browse.
        # Populated via :meth:`set_static_leader`; consumed by
        # :meth:`_maybe_wait_for_leader` and
        # :meth:`wait_for_leader_stopped`. ``None`` means "use mDNS"
        # (the normal LAN path). Mirrors :attr:`_static_peers` but in
        # the follower→leader direction; same macOS loopback rationale.
        self._static_leader: Optional[SessionAnnouncement] = None
        # Control plane (HTTP server) — populated on start() when role is set.
        self._control_plane: Optional[Any] = None
        # Cached capability snapshot — set by _start_control_plane() before
        # the HTTP server begins serving requests so adapter properties can
        # answer route handlers without acquiring self._lock. Default False
        # so any read before the control plane spins up is well-defined and
        # never raises AttributeError. See _ControlPlaneOrchestratorAdapter.
        self._has_audio_stream_at_start: bool = False
        # Session-global config this leader distributed (Phase 4).
        self._applied_session_config = None

        self._output_dir = self._compute_initial_output_dir()
        self._sync_tone = sync_tone or SyncToneConfig.default()
        self._chirp_player = chirp_player or create_default_player()
        self._streams: Dict[str, Stream] = {}
        self._state = SessionState.IDLE
        self._lock = threading.RLock()

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

        # Snapshot of the most recently completed episode path. Populated
        # by ``_prepare_next_episode()`` so callers can locate the files
        # they just wrote, even after ``output_dir`` has rotated to the
        # next episode. ``None`` until at least one recording has finished.
        self._last_episode_dir: Optional[Path] = None

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

        # Multi-host: start control plane + advertiser (or follower browser)
        # at construction time so cluster discovery is independent of device
        # lifecycle. Failures here raise out of __init__ (the constructor is
        # the right place to fail loudly on port-binding or role-config errors).
        if self._role is not None:
            self._bring_multihost_online()
            import atexit
            atexit.register(self._safe_shutdown)

    def _bring_multihost_online(self) -> None:
        """Spin up control plane + advertiser (or follower browser) at construction time.

        Audio-stream validation is deliberately NOT done here — streams are
        added AFTER __init__. The check stays in start().

        Failures partway through tear down anything that came up so the
        constructor doesn't leak threads/sockets.
        """
        started_control_plane = False
        try:
            self._start_control_plane()
            started_control_plane = True
            self._maybe_start_advertising()
            self._maybe_start_session_browser()
            self._maybe_start_follower_browser_in_background()
        except Exception:
            if self._advertiser is not None:
                try:
                    self._advertiser.close()
                except Exception:
                    pass
                self._advertiser = None
            if self._browser is not None:
                try:
                    self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if started_control_plane:
                self._force_stop_control_plane()
            raise

    def _safe_shutdown(self) -> None:
        """atexit-callable wrapper around shutdown() that swallows errors."""
        try:
            self.shutdown()
        except Exception:
            pass

    def shutdown(self) -> None:
        """Tear down ALL multi-host machinery (control plane + advertiser + browser).

        Safe to call multiple times. Single-host sessions (role=None) are
        no-ops. Devices are NOT touched here — call disconnect() for that.

        Automatically called via atexit when the orchestrator is constructed
        with a role; you usually don't need to call it explicitly unless
        you want deterministic teardown before process exit.
        """
        if self._role is None:
            return
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception as exc:
                logger.warning("browser.close failed: %s", exc)
            self._browser = None
        if self._advertiser is not None:
            try:
                self._advertiser.close()
            except Exception as exc:
                logger.warning("advertiser.close failed: %s", exc)
            self._advertiser = None
        self._force_stop_control_plane()
        self._observed_leader = None
        self._follower_observer_thread = None

    def _compute_initial_output_dir(self) -> Path:
        """Return the episode directory path for this session.

        - Single-host (``role is None``): ``<data_root>/ep_*`` (unchanged).
        - Multi-host: ``<data_root>/<session_id>/<host_id>/ep_*``.

        For a :class:`FollowerRole` that was constructed without a
        ``session_id`` (auto-discover mode), the real id is unknown until
        :meth:`start` observes the leader. A stable placeholder
        (``_pending_session``) is used so that streams registered before
        ``start()`` still have a predictable path; :meth:`start` calls
        :meth:`_rewrite_output_dir_for_observed_session` to rewrite the
        path once the id is known.
        """
        if self._role is None:
            return _generate_episode_path(self._data_root)

        session_id = self._resolve_session_id_for_path()
        return _generate_multihost_episode_path(
            self._data_root,
            session_id=session_id,
            host_id=self._host_id,
        )

    def _resolve_session_id_for_path(self) -> str:
        """Return the session_id to embed in the output path.

        Leaders and pre-shared followers know the id immediately. Auto-
        discover followers use ``_pending_session`` until the leader is
        observed; once ``_observed_leader`` is populated, subsequent
        calls (e.g. from ``_prepare_next_episode`` for a second episode)
        return the real session_id instead of the placeholder.
        """
        if isinstance(self._role, LeaderRole):
            assert self._role.session_id is not None  # set in __post_init__
            return self._role.session_id
        if isinstance(self._role, FollowerRole) and self._role.session_id is not None:
            return self._role.session_id
        if (
            isinstance(self._role, FollowerRole)
            and self._observed_leader is not None
        ):
            return self._observed_leader.session_id
        return _PENDING_SESSION_PLACEHOLDER

    def _rewrite_output_dir_for_observed_session(self) -> None:
        """Recompute ``self._output_dir`` now that the real session_id is known.

        Called after a :class:`FollowerRole` finishes observing the leader
        inside :meth:`start`. No-op for leaders, pre-shared followers, and
        single-host sessions — they already know the correct path at
        ``__init__`` time.
        """
        if not isinstance(self._role, FollowerRole):
            return
        if self._role.session_id is not None:
            return  # pre-shared id already embedded at init
        if self._observed_leader is None:
            return  # nothing to rewrite against

        real_session_id = self._observed_leader.session_id
        self._output_dir = _generate_multihost_episode_path(
            self._data_root,
            session_id=real_session_id,
            host_id=self._host_id,
        )
        self._rebind_stream_output_dirs()

    def _build_session_config(self):
        """Build the SessionConfig this leader will distribute to followers.

        Returns ``None`` for single-host sessions and for followers —
        only the leader constructs cluster-wide config. The config draws
        from ``self._sync_tone`` (chirp specs), ``self.session_id``
        (session name), and Phase-4's fixed ``recording_mode='standard'``.
        """
        from syncfield.multihost.session_config import SessionConfig

        if not isinstance(self._role, LeaderRole):
            return None
        return SessionConfig(
            session_name=self.session_id,
            start_chirp=self._sync_tone.start_chirp,
            stop_chirp=self._sync_tone.stop_chirp,
            recording_mode="standard",
        )

    def set_static_peers(
        self,
        peers: list,
    ) -> None:
        """Inject explicit peer addresses, bypassing mDNS discovery.

        Each peer is a ``SessionAnnouncement``-shaped dict or instance:

            session.set_static_peers([
                {"host_id": "mac_b", "control_plane_port": 7879,
                 "resolved_address": "127.0.0.1", "status": "preparing"},
                {"host_id": "mac_c", "control_plane_port": 7880,
                 "resolved_address": "127.0.0.1", "status": "preparing"},
            ])

        Used by the local single-machine test cluster
        (``scripts/multihost_local_cluster/leader.py``) to work around
        macOS's loopback mDNS resolution limitation, where
        ``zeroconf.get_service_info`` cannot retrieve TXT records for
        services on the same machine. Real LAN deployments with
        separate hosts use mDNS as normal.

        When set, ``_discover_followers_in_preparing`` and
        ``collect_from_followers`` use these peers IN ADDITION to any
        mDNS-discovered peers (deduped by host_id, static wins).

        Pass ``[]`` to clear.
        """
        from syncfield.multihost.types import SessionAnnouncement
        from importlib.metadata import version as _pkg_version

        normalized = []
        for p in peers:
            if isinstance(p, SessionAnnouncement):
                normalized.append(p)
            else:
                # Build SessionAnnouncement from dict, with sensible defaults.
                normalized.append(SessionAnnouncement(
                    session_id=self.session_id or p.get("session_id", "unknown"),
                    host_id=p["host_id"],
                    status=p.get("status", "preparing"),
                    sdk_version=p.get("sdk_version", _pkg_version("syncfield")),
                    chirp_enabled=p.get("chirp_enabled", True),
                    control_plane_port=p["control_plane_port"],
                    resolved_address=p.get("resolved_address", "127.0.0.1"),
                ))
        self._static_peers = normalized

    def set_static_leader(
        self,
        host_id: str,
        address: str,
        control_plane_port: int,
    ) -> None:
        """Tell this follower to bypass mDNS and poll the leader directly.

        The follower's ``start()`` would normally use a SessionBrowser to
        watch for the leader's status flip to ``"recording"``. On macOS
        loopback that doesn't work — zeroconf can't resolve TXT records
        for services on the same machine. Static leader mode swaps the
        browser for HTTP polling on the leader's existing /health endpoint.

        Used by ``scripts/multihost_local_cluster/follower.py`` for
        single-machine testing. Production multi-host (separate machines
        on a real LAN) uses mDNS as normal.
        """
        from syncfield.multihost.types import SessionAnnouncement
        from importlib.metadata import version as _pkg_version

        self._static_leader = SessionAnnouncement(
            session_id=self._role.session_id if self._role else "unknown",
            host_id=host_id,
            status="preparing",
            sdk_version=_pkg_version("syncfield"),
            chirp_enabled=True,
            control_plane_port=control_plane_port,
            resolved_address=address,
        )

    def _discover_followers_in_preparing(self) -> "list[SessionAnnouncement]":
        """Return the announcements of followers in the same session in 'preparing'.

        Skips self (matches on ``host_id``), ignores announcements from
        other sessions, and excludes any follower that did not advertise
        a control-plane port (those are structurally un-POSTable and
        would just become rejections downstream). Returns an empty list
        if no browser is attached (leader path uses an advertiser, not
        a browser — so the leader must separately start a short-lived
        browser for discovery;
        :meth:`_distribute_config_to_followers` takes care of that
        bootstrap).

        Static peers (set via :meth:`set_static_peers`) take precedence
        over mDNS discovery. When static peers are provided, mDNS is
        not consulted at all.
        """
        if self._static_peers:
            # Static peers bypass the macOS loopback mDNS limitation.
            return [p for p in self._static_peers if p.host_id != self._host_id]
        if self._browser is None:
            return []
        sid = self.session_id
        return [
            ann
            for ann in self._browser.current_sessions()
            if ann.session_id == sid
            and ann.host_id != self._host_id
            and ann.status == "preparing"
            and ann.control_plane_port is not None
        ]

    @staticmethod
    def _follower_base_url(ann) -> str:
        """Return ``http://<addr>:<port>`` for a follower's control plane.

        Uses the resolved LAN address from the mDNS ServiceInfo when
        present, else falls back to loopback. The fallback is what keeps
        localhost tests working — in production, every peer's advertisement
        carries an IPv4 address from the same LAN.
        """
        address = ann.resolved_address or "127.0.0.1"
        return f"http://{address}:{ann.control_plane_port}"

    def _distribute_config_to_followers(self) -> None:
        """POST this leader's SessionConfig to every preparing follower.

        No-op if no followers are currently visible. Follower 400 errors
        aggregate into a :class:`~syncfield.multihost.errors.ClusterConfigMismatch`,
        which the caller (``start()``) treats as a fatal start failure.
        Non-400 HTTP errors (timeouts, 5xx, connection refused) are
        also aggregated into the same exception with a synthesized
        reason so a single unreachable follower can't silently proceed.
        """
        import httpx
        import time as _time
        from syncfield.multihost.browser import SessionBrowser
        from syncfield.multihost.errors import ClusterConfigMismatch

        config = self._build_session_config()
        if config is None:
            return  # single-host or follower — nothing to distribute

        # The leader's long-lived browser was started in
        # :meth:`_bring_multihost_online`; by the time start() runs,
        # it has usually already resolved any followers that were up
        # when this process launched. Only bootstrap a short-lived
        # browser as a fallback if the long-lived one isn't present
        # (e.g. static-peers mode skips it, but _discover_followers_in_preparing
        # short-circuits to the static list and never touches the browser).
        leader_owned_browser = False
        if self._browser is None and not self._static_peers:
            self._browser = SessionBrowser(session_id=self.session_id)
            self._browser.start()
            leader_owned_browser = True
            _time.sleep(1.5)

        try:
            followers = self._discover_followers_in_preparing()
            if not followers:
                # No followers means the leader's config is still
                # ground truth for its own recording — persist it.
                self._applied_session_config = config
                return

            payload = config.to_dict()
            headers = {"Authorization": f"Bearer {self.session_id}"}
            rejections: dict = {}

            for ann in followers:
                if ann.control_plane_port is None:
                    # Filtered upstream by _discover_followers_in_preparing;
                    # defense-in-depth — turn into an assertion so a
                    # contract regression fails loudly.
                    assert False, (
                        "discovery contract loosened: got None control_plane_port"
                    )
                url = f"{self._follower_base_url(ann)}/session/config"
                # NOTE: Phase 5 plumbs the follower's real LAN address via
                # SessionAnnouncement.resolved_address (populated by the
                # browser from the mDNS ServiceInfo). When that field is
                # unset (e.g. localhost tests), _follower_base_url falls
                # back to 127.0.0.1 so the loopback path keeps working.
                try:
                    resp = httpx.post(url, json=payload, headers=headers, timeout=5.0)
                except httpx.HTTPError as exc:
                    rejections[ann.host_id] = f"network error: {exc}"
                    continue
                if resp.status_code != 200:
                    try:
                        detail = resp.json().get("detail", f"HTTP {resp.status_code}")
                    except Exception:
                        detail = f"HTTP {resp.status_code}"
                    rejections[ann.host_id] = str(detail)

            if rejections:
                raise ClusterConfigMismatch(rejections)

            # Remember what we successfully distributed so stop() can embed
            # it in the leader's own manifest.
            self._applied_session_config = config
        finally:
            if leader_owned_browser and self._browser is not None:
                try:
                    self._browser.close()
                except Exception as exc:  # pragma: no cover
                    logger.warning("Leader-owned browser close failed: %s", exc)
                self._browser = None

    def collect_from_followers(
        self,
        destination: Optional[Path] = None,
        *,
        timeout: float = 600.0,
        verify_checksums: bool = True,
        keep_leader_originals: bool = False,
    ) -> dict:
        """Aggregate every host's recorded files into one flat episode directory.

        Leader-only. Must be called **after** :meth:`stop` and **inside**
        the follower's ``keep_alive_after_stop_sec`` window — otherwise
        the follower control planes will already have torn themselves
        down and the GETs will fail with ``status="unreachable"``.

        Layout produced::

            <destination_root>/
                aggregated_manifest.json
                <leader_episode_name>/
                    <host_id>.<flat_filename>      (one entry per file per host)

        Each host's files are flattened into the leader's episode
        directory with a ``<host_id>.`` prefix; subdirectories within
        a host's episode become dotted segments (e.g. ``subdir/file.bin``
        → ``mac_b.subdir.file.bin``). The episode directory name itself
        is stripped from each host's path (it differs per host because
        each host generates its own timestamp) — the leader's episode
        name is the canonical one for the aggregated tree, matching what
        the downstream sync pipeline expects.

        The leader's own recordings (written during recording at
        ``<data_root>/<session>/<leader_host>/<ep>/``) are copied in
        first; followers are then pulled over HTTP. One bad follower
        never aborts collection from the rest.

        Args:
            destination: Directory to write into. Defaults to
                ``<data_root>/<session_id>/``. The leader's episode
                name is ALWAYS appended as the inner directory; the
                destination argument names the root that contains it.
            timeout: Per-host HTTP timeout in seconds. Defaults to 600
                because individual session files (multi-GB video) can
                take a while to stream over LAN.
            verify_checksums: sha256-verify each downloaded file
                against the manifest entry, with one retry on mismatch.
                Self-copied files are not re-hashed (trusted local).
            keep_leader_originals: When False (default), the leader's
                per-host subdir (``<root>/<session>/<leader_host>/``) is
                removed after a successful copy so the canonical layout
                is single-rooted. Set True to keep the originals for
                debugging.

        Returns:
            The aggregated manifest dict (same shape that gets written
            to ``<destination>/aggregated_manifest.json``).

        Raises:
            RuntimeError: If this orchestrator is not a leader, or if
                no ``session_id`` is set yet (called before ``start()``).
        """
        import json
        import time as _time

        if self._role is None or not isinstance(self._role, LeaderRole):
            raise RuntimeError(
                "collect_from_followers() requires a LeaderRole"
            )
        if self.session_id is None:
            raise RuntimeError(
                "collect_from_followers() requires an active session_id"
            )

        # Default destination root = <data_root>/<session_id>/. The
        # leader's episode name is always appended below.
        if destination is None:
            destination_root = self._data_root / self.session_id
        else:
            destination_root = Path(destination)
        destination_root.mkdir(parents=True, exist_ok=True)

        # Canonical episode dir name from the leader's own _output_dir.
        # This is the directory recording wrote into during Phase 1,
        # e.g. ``ep_20260413_013803_fe7d89``. Every host's files land
        # flat inside it.
        leader_episode_name = self._output_dir.name
        episode_dir = destination_root / leader_episode_name
        episode_dir.mkdir(parents=True, exist_ok=True)

        aggregate: dict = {
            "session_id": self.session_id,
            "leader_host_id": self._host_id,
            "leader_episode": leader_episode_name,
            "hosts": [],
        }

        # Step 1: copy the leader's own files in first so aggregate
        # ordering is leader → followers.
        aggregate["hosts"].append(
            self._copy_leader_files_to_episode_dir(
                episode_dir, keep_leader_originals
            )
        )

        # Step 2: discover followers via static peers (loopback escape
        # hatch) or a short-lived mDNS browser, mirroring the discovery
        # path used by :meth:`_distribute_config_to_followers`.
        browser_bootstrap: Optional["SessionBrowser"] = None
        if self._static_peers:
            peers = [
                p for p in self._static_peers if p.host_id != self._host_id
            ]
        else:
            browser_bootstrap = SessionBrowser(session_id=self.session_id)
            browser_bootstrap.start()
            # Let zeroconf converge; mDNS re-broadcasts every few
            # seconds by default, so ~1.5s is usually enough to see
            # every peer that's still up inside the keep-alive window.
            _time.sleep(1.5)
            peers = [
                ann
                for ann in browser_bootstrap.current_sessions()
                if ann.host_id != self._host_id
                and ann.control_plane_port is not None
            ]

        # Step 3: pull files from each follower into the flat dir.
        try:
            for ann in peers:
                aggregate["hosts"].append(
                    self._pull_follower_into_episode_dir(
                        ann,
                        episode_dir,
                        timeout=timeout,
                        verify_checksums=verify_checksums,
                    )
                )
        finally:
            if browser_bootstrap is not None:
                try:
                    browser_bootstrap.close()
                except Exception as exc:  # pragma: no cover - best-effort
                    logger.warning(
                        "collect_from_followers browser close failed: %s",
                        exc,
                    )

        # Step 4: write aggregated_manifest.json at the destination root.
        manifest_path = destination_root / "aggregated_manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(aggregate, f, indent=2)
        return aggregate

    def _copy_leader_files_to_episode_dir(
        self, episode_dir: Path, keep_originals: bool
    ) -> dict:
        """Copy the leader's own recorded files into the canonical episode dir.

        Source: ``self._output_dir`` (the leader's own recording path,
        layout set by Phase 1: ``<data_root>/<session_id>/<host_id>/ep_<ts>_<hex>/``).
        Target: ``episode_dir / <host_id>.<flat_path>``.

        Subdirectories within the source are flattened with ``.``
        separators. Self-copied files are trusted; no sha256 is computed
        for verification, though the digest is recorded in the report
        for downstream consumers.
        """
        import shutil

        if not self._output_dir.exists():
            return {
                "host_id": self._host_id,
                "status": "missing",
                "files": [],
                "error": "leader output dir does not exist",
            }

        copied: list = []
        for src in sorted(self._output_dir.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(self._output_dir).as_posix()
            flat = f"{self._host_id}.{rel.replace('/', '.')}"
            dst = episode_dir / flat
            shutil.copy2(src, dst)
            st = dst.stat()
            copied.append(
                {
                    "path": flat,
                    "size": st.st_size,
                    "sha256": _sha256_of_local(dst),
                    "mtime_ns": st.st_mtime_ns,
                }
            )

        if not keep_originals:
            # Remove the leader's ``<data_root>/<session_id>/<leader_host>/``
            # tree — the directory immediately above _output_dir's
            # episode dir — so the canonical layout is single-rooted.
            leader_host_dir = self._output_dir.parent
            if leader_host_dir.exists():
                try:
                    shutil.rmtree(leader_host_dir)
                except OSError as exc:
                    logger.warning(
                        "Failed to remove leader's host dir %s: %s",
                        leader_host_dir,
                        exc,
                    )

        return {
            "host_id": self._host_id,
            "status": "ok",
            "files": copied,
            "error": None,
        }

    def _pull_follower_into_episode_dir(
        self,
        ann,
        episode_dir: Path,
        *,
        timeout: float,
        verify_checksums: bool,
    ) -> dict:
        """Pull every file from a single follower into the flat episode dir.

        The follower's manifest paths are relative to its
        ``host_output_dir``, typically beginning with ``ep_<ts>_<hex>/``.
        We strip that leading episode segment and use only the
        stream-level remainder, then flatten any further subdirs with
        ``.`` so each file lives at
        ``episode_dir / <host_id>.<flat_path>``.

        Per-host failures (network errors, HTTP errors, checksum
        mismatches) populate ``status`` and ``error`` on the returned
        report; the outer loop moves on to the next host.
        """
        import httpx

        host_report: dict = {
            "host_id": ann.host_id,
            "status": "ok",
            "files": [],
            "error": None,
        }
        base = self._follower_base_url(ann)
        headers = {"Authorization": f"Bearer {self.session_id}"}

        # Step 1: fetch manifest. A failure here means we can't know
        # what to download — mark unreachable and return.
        try:
            manifest_resp = httpx.get(
                f"{base}/files/manifest",
                headers=headers,
                timeout=timeout,
            )
            manifest_resp.raise_for_status()
            entries = manifest_resp.json().get("files", [])
        except httpx.HTTPError as exc:
            host_report["status"] = "unreachable"
            host_report["error"] = f"manifest fetch: {exc}"
            return host_report

        # Step 2: stream each file. Use raw_rel against the follower's
        # /files/ endpoint (that's what the follower expects — relative
        # to its host_output_dir, including the ``ep_*/`` prefix); use
        # flat_name for the on-disk destination in the aggregated tree.
        for entry in entries:
            raw_rel = entry["path"]
            flat_name = self._flatten_follower_path(ann.host_id, raw_rel)
            target = episode_dir / flat_name
            expected_sha = entry.get("sha256")

            attempts_left = 2 if verify_checksums else 1
            last_error: Optional[str] = None
            while attempts_left > 0:
                attempts_left -= 1
                try:
                    with httpx.stream(
                        "GET",
                        f"{base}/files/{raw_rel}",
                        headers=headers,
                        timeout=timeout,
                    ) as r:
                        r.raise_for_status()
                        with open(target, "wb") as fout:
                            for chunk in r.iter_bytes(chunk_size=65536):
                                fout.write(chunk)
                except httpx.HTTPError as exc:
                    host_report["status"] = "error"
                    host_report["error"] = f"download {raw_rel}: {exc}"
                    last_error = host_report["error"]
                    break

                if not verify_checksums or expected_sha is None:
                    break
                actual = _sha256_of_local(target)
                if actual == expected_sha:
                    break
                if attempts_left == 0:
                    host_report["status"] = "checksum_mismatch"
                    host_report["error"] = f"sha256 mismatch on {raw_rel}"
                    last_error = host_report["error"]
                    break
                # else: loop and retry once more.

            if host_report["status"] != "ok":
                # First failure for this host wins the report;
                # don't attempt the rest of its files.
                break

            st = target.stat()
            host_report["files"].append(
                {
                    "path": flat_name,
                    "size": st.st_size,
                    "sha256": entry.get("sha256"),
                    "mtime_ns": st.st_mtime_ns,
                }
            )

        return host_report

    @staticmethod
    def _flatten_follower_path(host_id: str, raw_rel: str) -> str:
        """Convert a follower-side relative path to the flat episode-dir name.

        Strips a leading ``ep_*/`` segment and replaces remaining ``/``
        with ``.`` so subdirectories become dotted name components.
        The resulting name is prefixed with ``<host_id>.`` so every
        host's files share a single flat directory without colliding.
        """
        parts = raw_rel.split("/", 1)
        if parts[0].startswith("ep_") and len(parts) > 1:
            stream_path = parts[1]
        else:
            stream_path = raw_rel
        return f"{host_id}.{stream_path.replace('/', '.')}"

    def _fetch_config_from_leader(self) -> None:
        """Follower-side: GET the leader's applied SessionConfig and apply locally.

        Called after :meth:`_rewrite_output_dir_for_observed_session`
        so ``self._observed_leader`` is populated. No-op if the
        observed leader didn't advertise a control_plane_port (older
        SDK, or control plane failed to bind), or if there's no
        observed leader at all (single-host path / pre-attach state).
        """
        import httpx
        from syncfield.multihost.session_config import (
            SessionConfig,
            validate_config_against_local_capabilities,
        )

        if self._observed_leader is None:
            return
        port = self._observed_leader.control_plane_port
        if port is None:
            return

        url = f"{self._follower_base_url(self._observed_leader)}/session/config"
        headers = {"Authorization": f"Bearer {self.session_id}"}
        try:
            resp = httpx.get(url, headers=headers, timeout=5.0)
        except httpx.HTTPError as exc:
            logger.warning("Follower fetch of leader config failed: %s", exc)
            return

        if resp.status_code == 404:
            # Leader hasn't posted its config yet — happens when a
            # follower observes the leader BEFORE the leader's own
            # distribute loop runs. That distribute call will reach
            # us via POST shortly.
            return
        if resp.status_code != 200:
            logger.warning(
                "Follower got HTTP %d fetching leader config", resp.status_code
            )
            return

        cfg = SessionConfig.from_dict(resp.json())
        try:
            validate_config_against_local_capabilities(
                cfg,
                has_audio_stream=any(
                    getattr(s, "kind", "custom") == "audio"
                    for s in self._streams.values()
                ),
                supported_audio_range_hz=(20.0, 20_000.0),
            )
        except ValueError as exc:
            # Local validation fails — this is a real disagreement,
            # not a race. Surface it so the caller can abort.
            from syncfield.multihost.errors import ClusterConfigMismatch
            raise ClusterConfigMismatch({self._host_id: str(exc)})

        self._applied_session_config = cfg

    def _rebind_stream_output_dirs(self) -> None:
        """Propagate ``self._output_dir`` into streams that cached it at ``add()`` time.

        Adapters cache their output directory (and derived video / audio
        file paths) when a stream is registered. When the session's
        output directory changes mid-flight — either because a multi-
        host follower observed its leader (:meth:`_rewrite_output_dir_for_observed_session`)
        or because a new episode was prepared (:meth:`_prepare_next_episode`)
        — each stream's cached path needs to be rewritten to the new
        directory. The attribute-level ``hasattr`` probing is duck-typed
        because different adapter families cache different fields; the
        contract is "if you cache it, we overwrite it."
        """
        for stream in self._streams.values():
            if hasattr(stream, "_output_dir"):
                stream._output_dir = self._output_dir
            if hasattr(stream, "_file_path"):
                stream._file_path = self._output_dir / f"{stream.id}.mp4"
            if hasattr(stream, "_mp4_path"):
                stream._mp4_path = self._output_dir / f"{stream.id}.mp4"
            if hasattr(stream, "_wav_path"):
                stream._wav_path = None

    def _validate_multihost_audio_requirement(self) -> None:
        """Raise ``ValueError`` if this multi-host host has no audio stream.

        The chirp-based inter-host alignment requires every host to have
        at least one audio-capable stream:

        - Leader: must record the chirp it plays so the hardware DAC
          timestamp can be recovered in post-processing.
        - Follower: must record the leader's chirp arriving through the
          air so audio cross-correlation can pin its clock.

        Single-host sessions impose no such requirement.
        """
        if self._role is None:
            return

        has_audio = any(stream.kind == "audio" for stream in self._streams.values())
        if has_audio:
            return

        raise ValueError(
            f"multi-host role '{self._role.kind}' on host "
            f"'{self._host_id}' requires at least one audio-capable "
            f"stream; none of {list(self._streams)} qualify. "
            f"Add a microphone stream (e.g. HostAudioStream) before "
            f"calling start()."
        )

    def _build_control_plane_adapter(self):
        """Return an object that the control-plane routes can consume.

        The routes read a handful of simple attributes and call four
        triggers. We expose them through an adapter object rather than
        letting the routes reach into ``SessionOrchestrator`` privates
        — this keeps the routes decoupled and the orchestrator surface
        intentional.
        """
        # Self-import: _ControlPlaneOrchestratorAdapter is defined at
        # the bottom of this same module. Resolving it lazily inside the
        # method (rather than at module-load time) avoids a forward-
        # reference issue during class body evaluation.
        from syncfield.orchestrator import _ControlPlaneOrchestratorAdapter

        return _ControlPlaneOrchestratorAdapter(self)

    def _start_control_plane(self) -> None:
        """Spin up the HTTP control plane on the configured port.

        Called from :meth:`start` when ``self._role`` is not ``None``,
        after audio validation, before the advertiser. Populates
        ``self._control_plane`` and reads back the actual port so the
        advertiser can publish it via mDNS.
        """
        from syncfield.multihost.control_plane import ControlPlaneServer

        assert self._role is not None  # called only in multi-host path
        role = self._role
        # Snapshot capability flags BEFORE the control plane starts
        # serving requests. This avoids the route handlers needing to
        # acquire self._lock while the main thread is holding it
        # (which would deadlock — start() holds the lock through
        # _maybe_wait_for_leader, and the leader's distribute POST
        # arrives on the uvicorn worker thread). See
        # _ControlPlaneOrchestratorAdapter.has_audio_stream for the
        # corresponding lock-free read.
        self._has_audio_stream_at_start = any(
            getattr(s, "kind", "custom") == "audio"
            for s in self._streams.values()
        )
        self._control_plane = ControlPlaneServer(
            orchestrator=self._build_control_plane_adapter(),
            preferred_port=getattr(role, "control_plane_port", 7878),
            keep_alive_after_stop_sec=getattr(
                role, "keep_alive_after_stop_sec", 600.0
            ),
        )
        self._control_plane.start()
        logger.info(
            "Control plane listening on :%d for session %s",
            self._control_plane.actual_port,
            self.session_id,
        )

    def _arm_control_plane_keep_alive(self) -> None:
        """Arm the keep-alive timer after a successful stop()."""
        if self._control_plane is not None:
            self._control_plane.arm_keep_alive_shutdown()

    def _force_stop_control_plane(self) -> None:
        """Tear the control plane down immediately (error-path cleanup)."""
        if self._control_plane is not None:
            try:
                self._control_plane.stop()
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning("ControlPlaneServer.stop() failed: %s", exc)
            self._control_plane = None

    def _rollback_after_distribute_failure(self) -> None:
        """Roll back the session state after config distribution fails.

        Called from :meth:`start` when
        :meth:`_distribute_config_to_followers` raises
        :class:`~syncfield.multihost.errors.ClusterConfigMismatch` (or
        any other exception). The session is currently in ``RECORDING``
        with streams actively writing; we unwind it to ``CONNECTED``
        so the caller can retry ``start()`` without re-opening
        hardware. The advertiser never flipped to ``recording``
        (distribute runs before that flip), so we only need to tear
        down the post-chirp / post-stream-start state.
        """
        # 1. Stop each stream that's currently recording. Must happen
        #    BEFORE closing writers so any in-flight samples are
        #    flushed before the file handles go away.
        recording_streams = list(self._streams.values())
        _rollback_stop_recording(recording_streams)

        # 2. Close sample writers — matches the happy stop() flow so
        #    trailing samples from rolled-back streams don't race a
        #    closed file.
        self._close_sample_writers()

        # 3. Tear down control plane + discovery ONLY in the auto-connect
        #    path; explicit-connect callers keep their connect()-time
        #    multi-host infrastructure live so they can retry start()
        #    without re-opening hardware or re-advertising on mDNS.
        if self._auto_connected:
            self._force_stop_control_plane()
            self._stop_discovery_on_failure()

        # 4. Reset sync_point / chirp state so a later retry doesn't
        #    reuse stale anchors.
        self._sync_point = None
        self._session_clock = None
        self._chirp_start = None
        self._chirp_stop = None

        # 5. Close the session log. The episode dir is left on disk —
        #    the operator may want to inspect what got written before
        #    the abort, and a subsequent start_new_episode will create
        #    a fresh one.
        if self._log_writer is not None:
            self._log_writer.close()
            self._log_writer = None
        self._episode_dir_created = False

        # 6. Transition: if start() auto-connected from IDLE, tear all
        #    the way back to IDLE with devices closed so the caller sees
        #    the same state they started with. Otherwise land in CONNECTED
        #    for an explicit-connect caller who can retry without
        #    re-opening hardware.
        if self._auto_connected:
            _rollback_disconnect_streams(self._connected_streams)
            self._connected_streams = []
            self._auto_connected = False
            self._transition(SessionState.IDLE)
        else:
            self._transition(SessionState.CONNECTED)

    # -- test integration helpers (invoked only by dedicated tests) -----

    def _start_control_plane_only_for_tests(self) -> None:
        """Bypass start() to exercise control-plane wiring in isolation."""
        if self._role is None:
            raise RuntimeError("role required")
        self._validate_multihost_audio_requirement()
        self._start_control_plane()

    def _stop_control_plane_only_for_tests(self) -> None:
        self._force_stop_control_plane()

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
        """Episode directory for the *next* recording.

        Starts as a fresh ``ep_*`` path at ``__init__`` time. After each
        ``stop()`` / ``cancel()`` it rotates to a brand new path so the
        next ``start()`` writes to a clean directory. To find the files
        a just-finished recording wrote, use :attr:`last_episode_dir`.
        """
        return self._output_dir

    @property
    def last_episode_dir(self) -> Optional[Path]:
        """Episode directory of the most recently completed recording.

        ``None`` until at least one ``start()`` → ``stop()`` cycle has
        finished. After that, this points at the episode folder the
        just-completed recording wrote into — use this (not
        :attr:`output_dir`) to locate ``sync_point.json``, ``manifest.json``
        and per-stream files once control returns from ``stop()``.
        """
        return self._last_episode_dir

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
        # Multihost role-aware policy downgrade for Go3S streams: the leader/
        # follower communicate over lab WiFi (mDNS). Switching the host adapter
        # to a camera AP during a session breaks coordination. Force on_demand
        # so aggregation runs only when explicitly triggered by the viewer.
        try:
            from syncfield.adapters.insta360_go3s import Go3SStream as _Go3SStream
            if (
                isinstance(stream, _Go3SStream)
                and stream._aggregation_policy == "eager"
                and isinstance(self._role, (LeaderRole, FollowerRole))
            ):
                stream._aggregation_policy = "on_demand"
                logger.info(
                    "Go3S stream %r: aggregation_policy downgraded eager→on_demand "
                    "for multihost role %r (lab WiFi must stay connected for mDNS)",
                    stream.id,
                    self._role.kind,
                )
        except ImportError:
            # Adapter not installed (no 'camera' extra); nothing to downgrade.
            pass

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

            # Validate audio requirement before we touch any cluster
            # state. Streams are added between __init__ and start(),
            # so this can't live in __init__ — it stays at recording
            # time where the stream set is final.
            self._validate_multihost_audio_requirement()

            # Reset cross-host signals before blocking on the leader —
            # a stale "set" from a prior recording would otherwise make
            # _wait_for_leader_recording_state return immediately.
            self._leader_recording_signal.clear()

            # Refresh the capability snapshot read by control-plane route
            # handlers (POST /session/config). The cache was initialized
            # to False when the control plane came up at __init__ (no
            # streams yet); the final stream set is known now, so update
            # it before any distribute POST can race a route handler.
            if self._role is not None:
                self._has_audio_stream_at_start = any(
                    getattr(s, "kind", "custom") == "audio"
                    for s in self._streams.values()
                )

            # NOTE: multi-host machinery (control plane, advertiser,
            # follower browser) was already brought up at __init__.
            # start() now focuses exclusively on the recording
            # transition so the cluster can form well before the
            # operator clicks Record.

            # Multi-host: wait for leader BEFORE touching the
            # filesystem, so an auto-discover follower never mkdirs
            # the `_pending_session` placeholder path.
            #
            # State stays CONNECTED through the (potentially minutes-
            # long) _maybe_wait_for_leader block — a follower sitting
            # idle with its devices open and its control plane up is
            # semantically "ready", not "preparing". Only once the
            # leader flips to recording do we transition to PREPARING
            # for the actual pre-record work (config fetch, stream
            # start, chirp). Leaders also go CONNECTED→PREPARING here,
            # but since _maybe_wait_for_leader is a no-op on the leader
            # path the transition is effectively immediate.
            try:
                self._maybe_wait_for_leader()
                self._transition(SessionState.PREPARING)
                self._rewrite_output_dir_for_observed_session()
                self._maybe_start_follower_advertising_post_observation()
                self._fetch_config_from_leader()
            except Exception:
                # Auto-connected sessions tear all the way back to IDLE
                # on multi-host failure, including the advertiser,
                # control plane, and follower browser that connect()
                # brought up. Explicit-connect sessions stay in
                # CONNECTED with the multi-host infrastructure still
                # live — the operator may simply have clicked Record
                # before every rig finished attaching, and a retry
                # shouldn't have to re-open devices or re-advertise.
                if self._auto_connected:
                    self._stop_discovery_on_failure()
                    self._force_stop_control_plane()
                    _rollback_disconnect_streams(self._connected_streams)
                    self._connected_streams = []
                    self._auto_connected = False
                    self._transition(SessionState.IDLE)
                    # After the Task 3 reorder, _log_writer is created
                    # post-discovery, so on this failure path it is
                    # typically None — keep the close() guard as defense-
                    # in-depth in case a retry ever leaves a writer behind.
                    if self._log_writer is not None:
                        self._log_writer.close()
                        self._log_writer = None
                else:
                    self._transition(SessionState.CONNECTED)
                raise

            # Create the episode directory on first recording. The path
            # is now final: leaders and pre-shared followers had it at
            # __init__; auto-discover followers had it rewritten above.
            if not self._episode_dir_created:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                self._episode_dir_created = True
                logger.info("Episode dir created: %s", self._output_dir)

            # Open session log now that the directory exists.
            if self._log_writer is None:
                self._log_writer = SessionLogWriter(self._output_dir)
                self._log_writer.open()

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

                # If the user took the legacy one-shot path through
                # IDLE, tear down devices + discovery + control plane
                # and land in IDLE to preserve 0.1 rollback semantics.
                # Explicit connect callers keep the multi-host
                # infrastructure (advertiser, control plane, follower
                # browser) live and stay in CONNECTED so they can
                # retry without re-opening hardware or re-advertising.
                if self._auto_connected:
                    self._stop_discovery_on_failure()
                    self._force_stop_control_plane()
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

            # Distribute the leader's SessionConfig to every preparing
            # follower before announcing 'recording'. A rejection from
            # any follower raises ClusterConfigMismatch; we locally
            # roll back streams + writers + control plane + discovery
            # (the advertiser stays at 'preparing' which is correct —
            # it never flipped) before re-raising so the operator
            # sees a clean abort.
            try:
                self._distribute_config_to_followers()
            except Exception:
                self._rollback_after_distribute_failure()
                raise

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
            if self._auto_connected:
                # Legacy one-shot path (IDLE → start → STOPPED). The
                # user never called connect() explicitly, so they also
                # won't call disconnect(); tear down EVERYTHING here,
                # including the multi-host infrastructure connect()
                # brought up on their behalf.
                self._transition(SessionState.STOPPED)
                if self._log_writer is not None:
                    self._log_writer.close()
                    self._log_writer = None
                _rollback_disconnect_streams(self._connected_streams)
                self._connected_streams = []
                self._stop_discovery_on_failure()
                self._auto_connected = False
            else:
                # Explicit connect path. The advertiser just flipped to
                # ``stopped`` and stays up so followers (and the viewer)
                # can still observe the cluster. The follower's browser
                # also stays up — if the operator starts another
                # recording, start() reuses both instead of re-bringing
                # them up. disconnect() is the only place that tears
                # multi-host infrastructure down in this path.
                if self._log_writer is not None:
                    self._log_writer.close()
                    self._log_writer = None
                self._transition(SessionState.CONNECTED)

            # Prepare a fresh episode path for the next recording.
            self._prepare_next_episode()

            # Control plane stays up for keep_alive_after_stop_sec seconds
            # so the leader (or the local viewer) can pull files / view
            # final metrics. DELETE /session preempts the timer.
            self._arm_control_plane_keep_alive()

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

            # Prepare a fresh episode path for the next recording.
            self._prepare_next_episode()

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

            # Multi-host infrastructure (advertiser, browser, control plane)
            # stays up across disconnect(). It was brought up at __init__
            # and is only torn down by shutdown() or the atexit handler.

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
                clock_domain = event.clock_domain or host_id
                if isinstance(writer, SensorWriter):
                    writer.write(
                        SensorSample(
                            frame_number=event.frame_number,
                            capture_ns=event.capture_ns,
                            channels=event.channels or {},
                            clock_source="host_monotonic",
                            clock_domain=clock_domain,
                            uncertainty_ns=event.uncertainty_ns,
                        )
                    )
                else:
                    # Video streams use the FrameTimestamp schema —
                    # which intentionally has no `channels` field.
                    # Adapter-specific scalars (e.g. quest_native_ns
                    # for Quest cameras) are forwarded as ``extras``
                    # so they land as top-level keys in the JSONL row
                    # alongside frame_number / capture_ns / etc.
                    writer.write(
                        FrameTimestamp(
                            frame_number=event.frame_number,
                            capture_ns=event.capture_ns,
                            clock_source="host_monotonic",
                            clock_domain=clock_domain,
                            uncertainty_ns=event.uncertainty_ns,
                            extras=dict(event.channels) if event.channels else {},
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
            session_config=(
                self._applied_session_config.to_dict()
                if self._applied_session_config is not None
                else None
            ),
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
    # Episode lifecycle
    # ------------------------------------------------------------------

    def _prepare_next_episode(self) -> None:
        """Generate a fresh episode path and update all stream adapters.

        Called after ``stop()`` and ``cancel()`` so the next ``start()``
        writes to a new directory. Every stream adapter that holds a
        reference to the output path is updated to the new location.

        The outgoing path is snapshotted into ``self._last_episode_dir``
        so callers can still locate the files they just finished writing
        — ``session.output_dir`` always points at the *next* episode
        once ``stop()`` returns.
        """
        if self._episode_dir_created:
            self._last_episode_dir = self._output_dir
        self._output_dir = self._compute_initial_output_dir()
        self._episode_dir_created = False

        self._rebind_stream_output_dirs()

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
        """Start mDNS advertisement for this host.

        Leaders advertise as soon as :meth:`start` enters ``PREPARING``.
        Followers also advertise — so the leader's discovery helpers
        inside :meth:`_distribute_config_to_followers` and
        :meth:`collect_from_followers` can find them — BUT only when
        their ``session_id`` is known. Auto-discover followers, whose
        session id is unknown until ``_maybe_wait_for_leader`` observes
        a leader, defer their advertiser start to
        :meth:`_maybe_start_follower_advertising_post_observation`.

        No-op for single-host sessions.
        """
        if self._role is None:
            return
        # Auto-discover follower pre-observation: session_id unknown.
        # Caller will re-invoke after _maybe_wait_for_leader.
        if self.session_id is None:
            return
        self._start_advertiser_now(self.session_id)

    def _start_advertiser_now(self, session_id: str) -> None:
        """Construct and start the :class:`SessionAdvertiser` now.

        Used by :meth:`_maybe_start_advertising` (leader and pre-shared
        follower paths) and by
        :meth:`_maybe_start_follower_advertising_post_observation`
        (auto-discover follower path).
        """
        graceful = getattr(self._role, "graceful_shutdown_ms", 1000)
        self._advertiser = SessionAdvertiser(
            session_id=session_id,
            host_id=self._host_id,
            sdk_version=_pkg_version("syncfield"),
            chirp_enabled=self._sync_tone.enabled,
            graceful_shutdown_ms=graceful,
            control_plane_port=(
                self._control_plane.actual_port
                if self._control_plane is not None
                else None
            ),
        )
        self._advertiser.start()

    def _maybe_start_session_browser(self) -> None:
        """Start a long-lived mDNS browser for any multi-host role.

        Leaders and followers both benefit from a persistent browser:

        - Leaders use it so the viewer's cluster panel
          (``/api/cluster/peers``) can render observed followers
          immediately, without bootstrapping a fresh browser per poll
          (which on macOS has to wait for the dns-sd subprocess
          fallback to resolve SRV/TXT records every call).
        - Followers use it both for the cluster panel and for the
          background observer thread spawned by
          :meth:`_maybe_start_follower_browser_in_background`.

        Filter: leaders know their session_id at construction time, so
        we scope the browser to it. Auto-discover followers pass
        ``session_id=None`` and learn it after observation.

        No-op for single-host sessions, and no-op if a browser is
        already running.
        """
        if self._role is None:
            return
        if self._browser is not None:
            return
        if isinstance(self._role, FollowerRole):
            filter_session_id = getattr(self._role, "session_id", None)
        else:
            filter_session_id = self.session_id
        browser = SessionBrowser(session_id=filter_session_id)
        browser.start()
        self._browser = browser

    def _maybe_start_follower_browser_in_background(self) -> None:
        """Spawn the follower's background leader-observation thread.

        Called from :meth:`_bring_multihost_online` AFTER
        :meth:`_maybe_start_session_browser` has created the shared
        browser. The observer thread blocks on
        :meth:`SessionBrowser.wait_for_observation` with
        ``timeout=float("inf")``. As soon as any matching leader
        appears (in any status), the observer records it on
        :attr:`_observed_leader` and, if this is an auto-discover
        follower that was waiting for a session_id, starts its own
        advertiser so the leader can discover it in return.

        No-op for leaders and single-host sessions.
        """
        if not isinstance(self._role, FollowerRole):
            return
        if self._browser is None:
            return  # _maybe_start_session_browser didn't run — defensive guard
        browser = self._browser

        def _watch_for_leader() -> None:
            # Daemon thread — must never raise into the interpreter top.
            # Any zeroconf / wait-condition error is logged and swallowed.
            try:
                ann = browser.wait_for_observation(timeout=float("inf"))
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning(
                    "follower background observation failed: %s", exc
                )
                return
            if ann is None:
                # wait_for_observation always returns a SessionAnnouncement
                # or raises; guard anyway so a future signature change
                # doesn't silently set _observed_leader to None.
                return
            self._observed_leader = ann
            # Auto-discover followers only learn their session_id now,
            # which unblocks their advertiser. For pre-shared followers
            # the advertiser is already running and this is a no-op.
            try:
                self._maybe_start_follower_advertising_post_observation()
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "follower advertiser start post-observation failed: %s",
                    exc,
                )

        t = threading.Thread(
            target=_watch_for_leader,
            name=f"follower-observer-{self._host_id}",
            daemon=True,
        )
        t.start()
        self._follower_observer_thread = t

    def _maybe_start_follower_advertising_post_observation(self) -> None:
        """Start a follower's advertiser after the leader is observed.

        For an auto-discover :class:`FollowerRole`, ``session_id`` only
        becomes known after the browser populates
        ``self._observed_leader``. Both the background observer thread
        (started in :meth:`connect`) and :meth:`start` call into this
        helper — the orchestrator's :class:`~threading.RLock` serializes
        those concurrent attempts so exactly one advertiser instance is
        constructed.

        No-op if the advertiser is already running (pre-shared follower
        path) or if this host isn't a follower at all.
        """
        if not isinstance(self._role, FollowerRole):
            return
        # Serialize concurrent callers (background observer vs
        # start()'s main thread). RLock is reentrant so nested calls
        # from within start() do not self-deadlock.
        with self._lock:
            if self._advertiser is not None:
                return  # already up (pre-shared or another caller won the race)
            if self.session_id is None:
                return  # observation hasn't landed yet; defensive guard
            self._start_advertiser_now(self.session_id)

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

        The browser itself is started at :meth:`connect` time (via
        :meth:`_maybe_start_follower_browser_in_background`) so
        operators see the cluster populate in the viewer before
        clicking Record. The background observer thread may have
        already set :attr:`_observed_leader` by the time this runs;
        in that case we only need to wait for the status to flip to
        ``recording``. No-op for leader and single-host sessions.

        Raises:
            TimeoutError: If no leader reaches ``recording`` before
                the deadline.
        """
        if not isinstance(self._role, FollowerRole):
            return

        # Static leader mode: poll /health instead of mDNS browse.
        # Same macOS loopback rationale as :attr:`_static_peers` but
        # in the opposite direction (follower→leader).
        if self._static_leader is not None:
            self._poll_static_leader_until_recording()
            # Populate _observed_leader as if mDNS had observed it so
            # downstream code (config POST, manifest pulls) has the
            # leader announcement it expects.
            self._observed_leader = self._static_leader
            return

        # Browser is normally started at construction time via
        # :meth:`_maybe_start_session_browser`. Guard defensively —
        # if a future code path skips that, start both the browser
        # and the observer thread now.
        if self._browser is None:
            self._maybe_start_session_browser()
            self._maybe_start_follower_browser_in_background()

        assert self._browser is not None  # set by the helper above

        timeout = getattr(self._role, "leader_wait_timeout_sec", 60.0)

        # If the background observer thread already populated
        # _observed_leader, we've already crossed Phase 1; skip straight
        # to waiting for RECORDING.
        if self._observed_leader is None:
            deadline = time.monotonic() + timeout
            # Phase 1: observe leader ASAP (any status). The background
            # thread is blocked on the same event with timeout=inf; we
            # race it to the match but with a bounded deadline so a
            # truly missing leader still surfaces as TimeoutError here.
            first = self._browser.wait_for_observation(timeout=timeout)
            self._observed_leader = first
            remaining = max(0.1, deadline - time.monotonic())
        else:
            remaining = timeout

        # Advertiser may still not be up if the background observer
        # raced us to setting _observed_leader but this main-thread
        # path hadn't yet reached post-observation (or the background
        # thread called it and got a stale None session_id). Calling
        # here is idempotent — the helper's `if self._advertiser is
        # not None: return` guard makes it a no-op on the second call.
        self._maybe_start_follower_advertising_post_observation()

        # Phase 2: wait for leader to actually reach recording status.
        recording_ann = self._wait_for_leader_recording_state(
            self._observed_leader, remaining
        )
        # Refresh to latest (status changed).
        self._observed_leader = recording_ann

    def _wait_for_leader_recording_state(
        self, leader: "SessionAnnouncement", timeout: float
    ) -> "SessionAnnouncement":
        """Wait for the observed mDNS leader to report ``state=recording``.

        Cross-network mDNS TXT updates are unreliable on consumer WiFi —
        APs with IGMP snooping, multicast rate-limiting, or band-split
        SSIDs routinely drop them between clients. The browser's
        ``wait_for_recording`` condition variable then never fires even
        though the leader HAS actually transitioned. Symptom: follower
        times out while the leader is happily recording.

        Fix: after the observation phase has already populated
        ``leader.control_plane_port`` and ``leader.resolved_address``,
        poll ``GET /health`` directly over LAN-unicast HTTP. That
        endpoint returns the authoritative orchestrator state and does
        not depend on mDNS multicast reaching us. The browser keeps
        running in parallel; we just stop trusting it as the sole
        wake-up signal for the status flip.

        Falls back to the browser's ``wait_for_recording`` when the
        observed leader advertised no control plane (older leader, or
        test doubles that don't populate those fields) so existing
        loopback paths and unit tests keep working.
        """
        if leader.control_plane_port is None or not leader.resolved_address:
            return self._browser.wait_for_recording(timeout=timeout)

        import httpx
        from dataclasses import replace as _dc_replace

        deadline = time.monotonic() + timeout
        base = f"http://{leader.resolved_address}:{leader.control_plane_port}"
        headers = {"Authorization": f"Bearer {leader.session_id}"}
        poll_interval = 0.5

        logger.info(
            "Follower polling leader /health at %s for state=recording "
            "(mDNS TXT updates can be dropped cross-WiFi)",
            base,
        )
        while time.monotonic() < deadline:
            # TCP signal path: leader's POST /session/config arrived,
            # which only happens after leader transitioned to RECORDING.
            # More reliable than /health polling because it does not
            # depend on the follower's HTTP request completing — the
            # leader pushed the event to us.
            if self._leader_recording_signal.is_set():
                logger.info(
                    "Leader %s reached RECORDING (via POST /session/config)",
                    leader.host_id,
                )
                return _dc_replace(leader, status="recording")
            try:
                r = httpx.get(
                    f"{base}/health", headers=headers, timeout=2.0
                )
                if r.status_code == 200:
                    state = r.json().get("state", "")
                    if state == "recording":
                        logger.info(
                            "Leader %s reached RECORDING (via /health poll)",
                            leader.host_id,
                        )
                        return _dc_replace(leader, status="recording")
                    if state == "stopped":
                        # Fast session wrapped up before we saw
                        # 'recording' — treat as observed and let the
                        # follower stop. ``idle``, ``connected``, and
                        # ``preparing`` are all PRE-recording states:
                        # keep polling. Treating them as "done" would
                        # cause the follower to race ahead of the
                        # leader and exit before any recording happened.
                        logger.info(
                            "Leader %s reached state=stopped without "
                            "'recording' seen; proceeding",
                            leader.host_id,
                        )
                        return _dc_replace(leader, status="stopped")
            except httpx.HTTPError as exc:
                logger.debug(
                    "leader /health poll transient error: %s", exc
                )
            time.sleep(poll_interval)

        raise TimeoutError(
            f"leader {leader.host_id} did not reach state=recording "
            f"within {timeout:.1f}s (HTTP poll on {base})"
        )

    def _poll_static_leader_until_recording(self) -> None:
        """Poll the static leader's /health until state == 'recording'.

        Honors the role's ``leader_wait_timeout_sec``. Polls every 0.3s.
        Raises :class:`TimeoutError` if the leader never reaches
        ``recording`` (or a terminal post-recording state) before the
        deadline expires.
        """
        import httpx
        import time as _time

        assert self._static_leader is not None
        leader = self._static_leader
        deadline = _time.monotonic() + getattr(
            self._role, "leader_wait_timeout_sec", 60.0
        )
        base = f"http://{leader.resolved_address}:{leader.control_plane_port}"
        headers = {"Authorization": f"Bearer {self.session_id}"}

        logger.info(
            "Static leader polling: waiting for %s on %s to reach 'recording'",
            leader.host_id,
            base,
        )
        while _time.monotonic() < deadline:
            try:
                r = httpx.get(f"{base}/health", headers=headers, timeout=2.0)
                if r.status_code == 200:
                    state = r.json().get("state", "")
                    if state == "recording":
                        logger.info(
                            "Static leader %s reached state=recording",
                            leader.host_id,
                        )
                        return
                    if state == "stopped":
                        # Leader wrapped up before we observed recording —
                        # this can happen on a fast/short session. Treat
                        # as 'leader done' and return so follower can stop.
                        # ``idle``/``connected``/``preparing`` are PRE-
                        # recording states — keep polling on those,
                        # otherwise the follower races ahead of the
                        # leader and exits with empty recordings.
                        logger.info(
                            "Static leader %s reached state=stopped "
                            "without recording — proceeding",
                            leader.host_id,
                        )
                        return
                elif r.status_code == 503:
                    # Pre-observation 503 — leader still attaching itself,
                    # which shouldn't happen for a leader role, but tolerate.
                    pass
            except httpx.HTTPError:
                # Leader not up yet, or transient — keep polling.
                pass
            _time.sleep(0.3)

        raise TimeoutError(
            f"static leader {leader.host_id} did not reach state=recording "
            f"within {getattr(self._role, 'leader_wait_timeout_sec', 60.0):.1f}s"
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

        # Constructing the session brings the browser up immediately (since
        # refactor 64dd0fd the mDNS surface is online at __init__). The
        # meaningful precondition for this call is therefore that a
        # recording is underway — the sync clock is set as part of start().
        if self._session_clock is None:
            raise RuntimeError(
                "wait_for_leader_stopped() requires an active recording; "
                "call start() first"
            )

        # Static leader mode: poll /health instead of using the browser.
        if self._static_leader is not None:
            self._poll_static_leader_until_stopped(timeout)
            return self._static_leader

        if self._browser is None:
            raise RuntimeError(
                "wait_for_leader_stopped() requires an active SessionBrowser; "
                "call start() first"
            )
        return self._wait_for_leader_stopped_state(timeout)

    def _wait_for_leader_stopped_state(
        self, timeout: float
    ) -> SessionAnnouncement:
        """HTTP-primary wait for the observed leader to reach 'stopped'.

        Mirrors :meth:`_wait_for_leader_recording_state`: mDNS TXT
        updates aren't guaranteed to cross WiFi, so we poll the leader's
        authoritative ``/health`` endpoint when we have its address.
        Falls back to the browser's ``wait_for_stopped`` only when the
        observed leader never advertised a control plane (legacy path).
        """
        leader = self._observed_leader
        if (
            leader is None
            or leader.control_plane_port is None
            or not leader.resolved_address
        ):
            return self._browser.wait_for_stopped(timeout=timeout)

        import httpx
        from dataclasses import replace as _dc_replace

        deadline = time.monotonic() + timeout
        base = f"http://{leader.resolved_address}:{leader.control_plane_port}"
        headers = {"Authorization": f"Bearer {leader.session_id}"}

        while time.monotonic() < deadline:
            try:
                r = httpx.get(
                    f"{base}/health", headers=headers, timeout=2.0
                )
                if r.status_code == 200:
                    state = r.json().get("state", "")
                    if state in ("stopped", "idle"):
                        return _dc_replace(leader, status="stopped")
                elif r.status_code in (404, 503):
                    # Control plane tearing down == stopped from our
                    # perspective.
                    return _dc_replace(leader, status="stopped")
            except httpx.HTTPError:
                # Control plane gone (keep-alive expired) == stopped.
                return _dc_replace(leader, status="stopped")
            time.sleep(0.5)

        raise TimeoutError(
            f"leader {leader.host_id} did not reach state=stopped within "
            f"{timeout:.1f}s (HTTP poll on {base})"
        )

    def _poll_static_leader_until_stopped(self, timeout: float) -> None:
        """Poll the static leader's /health until it reports stopped/idle.

        A connection refusal or a 404/503 response is also treated as
        "leader is gone" — once the leader's keep-alive window expires
        the control plane shuts down, and from a follower's perspective
        that's indistinguishable from (and equivalent to) ``stopped``.
        """
        import httpx
        import time as _time

        assert self._static_leader is not None
        leader = self._static_leader
        deadline = _time.monotonic() + timeout
        base = f"http://{leader.resolved_address}:{leader.control_plane_port}"
        headers = {"Authorization": f"Bearer {self.session_id}"}

        while _time.monotonic() < deadline:
            try:
                r = httpx.get(f"{base}/health", headers=headers, timeout=2.0)
                if r.status_code == 200:
                    state = r.json().get("state", "")
                    if state in ("stopped", "idle"):
                        return
                else:
                    # Non-200: leader's control plane probably already
                    # shut down, which (in keep_alive context) also
                    # means stopped.
                    if r.status_code in (404, 503):
                        return
            except httpx.HTTPError:
                # Connection refused -> leader's control plane is down
                # -> stopped (graceful: no leader = no recording).
                return
            _time.sleep(0.5)

        raise TimeoutError(
            f"static leader {leader.host_id} did not reach state=stopped "
            f"within {timeout:.1f}s"
        )


    # ------------------------------------------------------------------
    # Aggregation control — Go3S on-demand / retry
    # ------------------------------------------------------------------

    def aggregate_episode(self, episode_id: str) -> None:
        """Trigger on-demand aggregation for an episode that is pending.

        Searches all registered streams for a Go3S stream whose
        ``pending_aggregation_job`` matches *episode_id* and enqueues
        that job on the global aggregation queue.

        Raises:
            RuntimeError: If the Go3S adapter is not installed.
            KeyError: If no matching pending job is found.
        """
        try:
            from syncfield.adapters.insta360_go3s.stream import (
                _enqueue_on_global_queue,
            )
        except ImportError:
            raise RuntimeError("Go3S adapter not installed (missing 'camera' extra)")

        for stream in self._streams.values():
            pending = getattr(stream, "pending_aggregation_job", None)
            if pending is not None and pending.episode_id == episode_id:
                _enqueue_on_global_queue(pending)
                return
        raise KeyError(f"No pending aggregation for episode {episode_id!r}")

    def retry_aggregation(self, job_id: str) -> None:
        """Re-enqueue a previously-failed aggregation job.

        Delegates to :meth:`AggregationQueue.retry` on the global queue.

        Raises:
            RuntimeError: If the Go3S adapter is not installed.
        """
        try:
            from syncfield.adapters.insta360_go3s.stream import _global_aggregation_queue
        except ImportError:
            raise RuntimeError("Go3S adapter not installed (missing 'camera' extra)")
        _global_aggregation_queue().retry(job_id)

    def cancel_aggregation(self, job_id: str) -> None:  # noqa: ARG002
        """Cancel an in-flight or queued aggregation job.

        Not implemented in v1 — raises ``NotImplementedError``.
        """
        raise NotImplementedError("cancel_aggregation deferred to v2")

    def aggregate_all_pending(self) -> dict:
        """Scan ``output_dir`` for episodes with pending aggregation manifests and enqueue them all.

        This is the "Sync now" path for the wrist-mount workflow: the user
        records multiple episodes while the Go3S camera's WiFi is off, then
        docks the camera, enables WiFi, and clicks a single button in the
        viewer to download every pending episode's file in one pass.

        Returns:
            dict with ``enqueued``: list of job ids, and ``skipped``: list of
            ``{episode_id, reason}`` for manifests that could not be enqueued.

        Raises:
            RuntimeError: If the Go3S adapter is not installed.
        """
        try:
            from syncfield.adapters.insta360_go3s.stream import (
                _enqueue_on_global_queue,
                _global_aggregation_queue,
            )
            from syncfield.adapters.insta360_go3s.aggregation.types import (
                AggregationState,
            )
        except ImportError:
            raise RuntimeError(
                "Go3S adapter not installed (missing 'camera' extra)"
            )

        queue = _global_aggregation_queue()
        # IMPORTANT: output_dir points at the CURRENT episode dir
        # (``ep_<timestamp>``). Aggregation manifests from PRIOR episodes
        # live in sibling directories under _data_root, so that's where we
        # must scan. recover_from_disk returns PENDING/RUNNING jobs and
        # normalizes RUNNING to PENDING so they're re-enqueued safely.
        search_root = getattr(self, "_data_root", self.output_dir)
        candidates = queue.recover_from_disk(search_root=search_root)

        enqueued: list[str] = []
        skipped: list[dict] = []
        for job in candidates:
            if job.state == AggregationState.COMPLETED:
                continue
            try:
                _enqueue_on_global_queue(job)
                enqueued.append(job.job_id)
            except Exception as e:
                skipped.append(
                    {"episode_id": job.episode_id, "reason": f"enqueue: {e}"}
                )
        return {"enqueued": enqueued, "skipped": skipped}


class _ControlPlaneOrchestratorAdapter:
    """Narrow adapter between SessionOrchestrator and FastAPI routes.

    Routes never touch ``SessionOrchestrator`` directly. They go through
    this adapter, which:

    - exposes a stable set of attributes (``host_id``, ``session_id``,
      ``role_kind``, ``state_name``, ``sdk_version``),
    - snapshots per-stream metrics into plain dataclass instances so
      the orchestrator's stream dict can keep mutating without confusing
      the route,
    - translates the route triggers (``trigger_start`` / ``trigger_stop``
      / ``trigger_control_plane_shutdown``) into the appropriate
      orchestrator methods.
    """

    def __init__(self, orchestrator: "SessionOrchestrator") -> None:
        self._orch = orchestrator

    # -- identity surface --

    @property
    def host_id(self) -> str:
        return self._orch.host_id

    @property
    def session_id(self) -> Optional[str]:
        """Current session_id, or ``None`` for an auto-discover follower
        that hasn't observed its leader yet.

        Requests received during the pre-observation window return 503
        via :func:`~syncfield.multihost.control_plane.auth.verify_session_token`
        — callers should retry after the follower has attached.
        """
        return self._orch.session_id

    @property
    def role_kind(self) -> Optional[str]:
        return self._orch._role.kind if self._orch._role is not None else None

    @property
    def state_name(self) -> str:
        return self._orch.state.value

    @property
    def sdk_version(self) -> str:
        from importlib.metadata import version

        return version("syncfield")

    @property
    def has_audio_stream(self) -> bool:
        """Read the cached capability snapshot — lock-free.

        Computed once at :meth:`SessionOrchestrator._start_control_plane`
        time so the route handlers serving Phase 4's
        ``POST /session/config`` don't need to acquire the orchestrator
        lock. The main thread holds ``self._lock`` for the duration of
        :meth:`SessionOrchestrator.start` (including
        ``_maybe_wait_for_leader``), so a cross-thread acquisition from
        the uvicorn worker would deadlock — leader's distribute POST
        times out, follower's POST handler stays blocked, follower
        never unblocks because the leader's status flip is delayed.
        """
        return getattr(self._orch, "_has_audio_stream_at_start", False)

    @property
    def supported_audio_range_hz(self):
        """Best-effort audio-frequency range supported by this host's audio streams.

        Phase 4 uses a conservative default (20 Hz - 20 kHz — the
        standard human-hearing range, which every consumer mic covers)
        since the adapter SPI does not yet expose per-stream frequency
        ranges. A future phase may tighten this once individual adapters
        publish their true capability ranges.
        """
        return (20.0, 20_000.0)

    # -- metrics snapshot --

    def snapshot_stream_metrics(self):
        from syncfield.multihost.control_plane.routes import StreamHealth  # type: ignore[attr-defined]  # noqa: F401

        # Pull whatever metrics each stream exposes. Phase 3 is
        # intentionally minimal — we report id/kind and fill the rest
        # with zeros when the stream doesn't track them. Future phases
        # can extend the stream contract with a dedicated metrics API.
        # TODO(phase-4+): extract a formal StreamMetrics Protocol on
        # the adapter contract instead of duck-typing into per-adapter
        # private fields. Current names ({_last_fps, _frame_count,
        # _dropped_count, _last_frame_ns, _bytes_written}) encode
        # an assumption that every adapter uses these exact private
        # attributes; silent zeros slip in otherwise.
        # Snapshot the stream list WITHOUT acquiring the orchestrator
        # lock — see ``has_audio_stream`` for the deadlock rationale.
        # ``_streams`` only mutates under ``add()``, which requires
        # IDLE state and rejects in any other state. During
        # PREPARING / RECORDING / STOPPED reads are race-free, and
        # ``list(dict.values())`` produces an atomic copy under the
        # GIL so a concurrent IDLE-time ``add()`` can't trip a
        # "dictionary changed size during iteration" here either.
        streams = list(self._orch._streams.values())

        out = []
        for stream in streams:
            out.append(
                _StreamMetricsSnapshot(
                    id=stream.id,
                    kind=getattr(stream, "kind", "custom"),
                    fps=float(getattr(stream, "_last_fps", 0.0) or 0.0),
                    frames=int(getattr(stream, "_frame_count", 0) or 0),
                    dropped=int(getattr(stream, "_dropped_count", 0) or 0),
                    last_frame_ns=getattr(stream, "_last_frame_ns", None),
                    bytes_written=int(getattr(stream, "_bytes_written", 0) or 0),
                )
            )
        return out

    # -- triggers --

    def trigger_start(self) -> str:
        """Idempotently start recording.

        **Blocks the uvicorn worker thread** for the full duration of
        :meth:`SessionOrchestrator.start` — countdown, chirp, log
        writer initialization, and device readiness. Callers hitting
        ``POST /session/start`` should set HTTP client timeouts
        comfortably above the expected countdown duration plus a few
        seconds for chirp emission and file I/O. A Phase-4 refactor
        is expected to move this to a worker pool so the HTTP request
        returns 202 immediately with the transition proceeding in
        the background.
        """
        from syncfield.types import SessionState

        if self._orch.state is SessionState.RECORDING:
            return self._orch.state.value
        self._orch.start()
        return self._orch.state.value

    def trigger_stop(self) -> str:
        """Idempotently stop recording.

        **Blocks the uvicorn worker thread** for the full duration of
        :meth:`SessionOrchestrator.stop` — chirp emission, finalization,
        and writer close. Same client-timeout guidance as
        :meth:`trigger_start`.
        """
        from syncfield.types import SessionState

        if self._orch.state in (SessionState.STOPPED, SessionState.IDLE):
            return self._orch.state.value
        self._orch.stop()
        return self._orch.state.value

    def trigger_control_plane_shutdown(self) -> None:
        # Defer the actual shutdown — the route is currently handling
        # a request on the uvicorn thread that would be killed. Schedule
        # it on a short delay.
        import threading

        timer = threading.Timer(0.05, self._orch._force_stop_control_plane)
        timer.daemon = True
        timer.start()

    def apply_distributed_config(self, config) -> None:
        """Propagate a POSTed SessionConfig to the orchestrator.

        Called from the POST /session/config handler after successful
        local validation so the follower's manifest.json embeds the
        same config the leader distributed. Mirrors what the leader
        does in _distribute_config_to_followers after a successful
        push.

        Also fires the follower's recording-signal event: the leader
        only POSTs this endpoint after transitioning its own state to
        ``RECORDING``, so the POST's arrival is proof that the leader
        is recording now. This unblocks
        :meth:`SessionOrchestrator._wait_for_leader_recording_state`
        via TCP regardless of whether the follower's HTTP /health
        poll or mDNS TXT observation had already seen the transition
        — both of which can be silently delayed on WiFi.
        """
        self._orch._applied_session_config = config
        self._orch._leader_recording_signal.set()

    def host_output_dir(self) -> Optional[Path]:
        """Return the directory holding this host's recorded files.

        For multi-host sessions this is ``<data_root>/<session_id>/<host_id>/``
        (the directory containing the ep_* episode dirs). Returns None if
        no episode has been recorded yet (orchestrator still IDLE or the
        episode dir was never created).
        """
        if not self._orch._episode_dir_created:
            return None
        # self._orch._output_dir is <data_root>/<session_id>/<host_id>/ep_*
        # — walk one level up to get the host directory.
        return self._orch._output_dir.parent


class _StreamMetricsSnapshot:
    """Tiny value object shape the routes accept (see `StreamHealth` in schemas)."""

    __slots__ = ("id", "kind", "fps", "frames", "dropped", "last_frame_ns", "bytes_written")

    def __init__(self, *, id, kind, fps, frames, dropped, last_frame_ns, bytes_written):
        self.id = id
        self.kind = kind
        self.fps = fps
        self.frames = frames
        self.dropped = dropped
        self.last_frame_ns = last_frame_ns
        self.bytes_written = bytes_written
