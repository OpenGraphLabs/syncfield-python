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
        # Control plane (HTTP server) — populated on start() when role is set.
        self._control_plane: Optional[Any] = None
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
        """
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

        # Leaders don't hold a browser during the session — advertiser only.
        # Bootstrap a short-lived browser here so we can discover followers
        # that are currently in the 'preparing' phase. Filter on our own
        # session_id so we only see our cluster.
        leader_owned_browser = False
        if self._browser is None:
            self._browser = SessionBrowser(session_id=self.session_id)
            self._browser.start()
            leader_owned_browser = True
            # Let zeroconf converge; mDNS advertisements re-broadcast
            # every few seconds by default, so ~1.5s is usually enough
            # to see every follower that's already up and advertising.
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
    ) -> dict:
        """Pull every follower's recorded files into a single canonical tree.

        Leader-only. Must be called **after** :meth:`stop` and **inside**
        the follower's ``keep_alive_after_stop_sec`` window — otherwise
        the follower control planes will already have torn themselves
        down and the GETs will fail with ``status="unreachable"``.

        For each follower discovered via mDNS this method:

        1. Hits ``GET /files/manifest`` with the session bearer token to
           list every file the follower wrote during the session.
        2. Streams each file via ``GET /files/{path}`` into
           ``destination/<host_id>/<path>`` (parent dirs created on
           demand).
        3. When ``verify_checksums`` is true, hashes each downloaded
           file with sha256 and compares to the manifest entry. On
           mismatch it re-downloads exactly **once**; if the second
           attempt also mismatches, the host is recorded with
           ``status="checksum_mismatch"``.

        Per-host failures (network errors, HTTP errors, checksum
        mismatches) populate the ``status`` and ``error`` fields on
        that host's report and the loop moves on — one bad follower
        never aborts collection from the rest.

        After every host is processed (or skipped on failure), an
        aggregated report is written to
        ``<destination>/aggregated_manifest.json`` and returned.

        Args:
            destination: Where to materialize the cluster tree. Defaults
                to ``self._data_root / self.session_id`` (the cluster
                root used by the rest of the multi-host plumbing).
                Created if missing.
            timeout: Per-HTTP-request timeout in seconds. Defaults to
                600 because individual session files (multi-GB video)
                can take a while to stream over LAN.
            verify_checksums: Whether to sha256-verify each downloaded
                file against the manifest entry. When false the loop
                still records files but performs no integrity check.

        Returns:
            An :class:`AggregatedManifest`-shaped ``dict`` with a
            per-host status entry (``"ok"`` / ``"unreachable"`` /
            ``"checksum_mismatch"`` / ``"error"``) and the file list
            that was successfully pulled for each host.

        Raises:
            RuntimeError: If this orchestrator is not a leader, or if
                no ``session_id`` is set yet (called before ``start()``).
        """
        import json
        import time as _time

        import httpx

        if self._role is None or not isinstance(self._role, LeaderRole):
            raise RuntimeError(
                "collect_from_followers() requires a LeaderRole"
            )
        if self.session_id is None:
            raise RuntimeError(
                "collect_from_followers() requires an active session_id"
            )

        if destination is None:
            destination = self._data_root / self.session_id
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)

        headers = {"Authorization": f"Bearer {self.session_id}"}
        hosts_report: list[dict] = []

        # Leaders don't keep a long-lived browser around the way
        # followers do — bootstrap a short-lived one here, mirroring
        # _distribute_config_to_followers. Filter on our session_id so
        # we only see this cluster's peers.
        browser = SessionBrowser(session_id=self.session_id)
        browser.start()
        try:
            # Let zeroconf converge; mDNS re-broadcasts every few
            # seconds by default, so ~1.5s is usually enough to see
            # every peer that's still up inside the keep-alive window.
            _time.sleep(1.5)

            peers = [
                ann
                for ann in browser.current_sessions()
                if ann.host_id != self._host_id
                and ann.control_plane_port is not None
            ]

            for ann in peers:
                host_report: dict = {
                    "host_id": ann.host_id,
                    "status": "ok",
                    "files": [],
                    "error": None,
                }
                base = self._follower_base_url(ann)

                # Step 1: fetch manifest. A failure here means we can't
                # know what to download — mark unreachable and move on.
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
                    hosts_report.append(host_report)
                    continue

                host_dest = destination / ann.host_id
                host_dest.mkdir(parents=True, exist_ok=True)

                # Step 2: stream each file. Track the first failing file
                # so the caller can investigate; subsequent files for
                # that host are skipped (the host is already marked as
                # broken, so additional downloads would just muddy the
                # report).
                for entry in entries:
                    rel = entry["path"]
                    target = host_dest / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    expected_sha = entry.get("sha256")
                    # One retry on checksum mismatch when verifying;
                    # otherwise a single attempt.
                    attempts_left = 2 if verify_checksums else 1
                    while attempts_left > 0:
                        attempts_left -= 1
                        try:
                            with httpx.stream(
                                "GET",
                                f"{base}/files/{rel}",
                                headers=headers,
                                timeout=timeout,
                            ) as r:
                                r.raise_for_status()
                                with open(target, "wb") as f:
                                    for chunk in r.iter_bytes(
                                        chunk_size=65536
                                    ):
                                        f.write(chunk)
                        except httpx.HTTPError as exc:
                            host_report["status"] = "error"
                            host_report["error"] = (
                                f"download {rel}: {exc}"
                            )
                            break

                        if not verify_checksums:
                            break
                        actual = _sha256_of_local(target)
                        if actual == expected_sha:
                            break
                        if attempts_left == 0:
                            host_report["status"] = "checksum_mismatch"
                            host_report["error"] = (
                                f"sha256 mismatch on {rel}"
                            )
                            break
                        # else: loop and retry once more.

                    if host_report["status"] != "ok":
                        # First failure for this host wins the report;
                        # don't attempt the rest of its files.
                        break
                    host_report["files"].append(entry)

                hosts_report.append(host_report)
        finally:
            try:
                browser.close()
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning(
                    "collect_from_followers browser close failed: %s",
                    exc,
                )

        aggregate = {
            "session_id": self.session_id,
            "leader_host_id": self._host_id,
            "hosts": hosts_report,
        }
        with open(destination / "aggregated_manifest.json", "w") as f:
            json.dump(aggregate, f, indent=2)
        return aggregate

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

        # 3. Tear down control plane + discovery.
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

            if self._role is not None:
                self._validate_multihost_audio_requirement()
                self._start_control_plane()

            # Multi-host: advertise PREPARING / wait for leader BEFORE
            # touching the filesystem, so an auto-discover follower
            # never mkdirs the `_pending_session` placeholder path.
            self._transition(SessionState.PREPARING)
            try:
                self._maybe_start_advertising()
                self._maybe_wait_for_leader()
                self._rewrite_output_dir_for_observed_session()
                self._fetch_config_from_leader()
            except Exception:
                self._stop_discovery_on_failure()
                self._force_stop_control_plane()
                # Auto-connected sessions tear all the way back to IDLE
                # on multi-host failure; explicit-connect sessions stay
                # in CONNECTED so the caller can retry without
                # re-opening hardware.
                if self._auto_connected:
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
                self._transition(SessionState.STOPPED)
                if self._log_writer is not None:
                    self._log_writer.close()
                    self._log_writer = None
                _rollback_disconnect_streams(self._connected_streams)
                self._connected_streams = []
                self._stop_discovery_on_failure()
                self._auto_connected = False
            else:
                # Close the log writer for this episode.
                if self._log_writer is not None:
                    self._log_writer.close()
                    self._log_writer = None
                self._transition(SessionState.CONNECTED)
                self._stop_discovery_on_failure()

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
        """
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
            control_plane_port=(
                self._control_plane.actual_port
                if self._control_plane is not None
                else None
            ),
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
    def session_id(self) -> str:
        sid = self._orch.session_id
        assert sid is not None, "control plane requires a session_id"
        return sid

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
        """Whether this host has at least one audio-capable stream registered."""
        with self._orch._lock:
            return any(
                getattr(stream, "kind", "custom") == "audio"
                for stream in self._orch._streams.values()
            )

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
        # Grab a list snapshot under the orchestrator lock so a
        # concurrent ``add()`` / ``_prepare_next_episode`` can't
        # raise "dictionary changed size during iteration" on the
        # uvicorn worker thread. We release the lock before building
        # the per-stream snapshots so their construction doesn't
        # serialize with session state transitions.
        with self._orch._lock:
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
        """
        self._orch._applied_session_config = config

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
