"""MetaQuestHandStream — Meta Quest 3 hand/head tracking via WiFi UDP.

Receives hand tracking data from a Unity app running on Quest 3.
Data includes 26 OpenXR hand joint positions, per-joint quaternion
rotations, and head pose — all streamed as JSON over UDP.

Usage::

    from syncfield.adapters import MetaQuestHandStream

    session.add(MetaQuestHandStream("quest3_hands"))
    # or with controller-as-wrist mode:
    session.add(MetaQuestHandStream("quest3_ctrl", mode="controller"))

The Quest app sends UDP packets to the configured port (default 14043).
Each packet is a JSON object::

    {
      "v": 1,
      "seq": 42,
      "ts_ms": 1234567890.123,
      "head": {"pos": [x,y,z], "rot": [x,y,z,w]},
      "left":  {"tracked": true, "joints": [{"pos":[...],"rot":[...]}, ...]},
      "right": {"tracked": true, "joints": [...]},
      "controllers": {
        "left":  {"tracked": true, "pos": [...], "rot": [...]},
        "right": {"tracked": true, "pos": [...], "rot": [...]}
      }
    }

Coordinate system: Unity left-handed Y-up (Quest's native OpenXR frame).
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from syncfield.clock import SessionClock
from syncfield.stream import StreamBase
from syncfield.types import (
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    StreamCapabilities,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenXR hand tracking constants
# ---------------------------------------------------------------------------

NUM_JOINTS = 26       # OpenXR XR_HAND_JOINT_COUNT_EXT (Palm → LittleTip)
NUM_COORDS = 3        # xyz position
NUM_QUAT = 4          # quaternion xyzw
NUM_HANDS = 2
WRIST_JOINT_INDEX = 1

JOINTS_DIM = NUM_JOINTS * NUM_COORDS * NUM_HANDS      # 156
ROTATIONS_DIM = NUM_JOINTS * NUM_QUAT * NUM_HANDS      # 208
HEAD_POSE_DIM = 7     # pos(3) + quat(4)
DEFAULT_PORT = 14043
DEFAULT_DISCOVERY_PORT = 14044

# Discovery protocol spoken by the Unity companion app
# (opengraph-studio/unity/SyncFieldQuest3Sender/Assets/Scripts/
#  UDPTrackingSender.cs). The Quest broadcasts the probe string on
# UDP :14044 every ~2 s; whoever responds with a well-formed
# RecorderDiscoveryResponse becomes the Quest's tracking target.
DISCOVERY_PROBE = b"SYNCFIELD_DISCOVER_RECORDER_V1"
DISCOVERY_RESPONSE_TYPE = "syncfield_recorder"


_QUEST_HTTP_PORT = 14045
_QUEST_IP_CACHE_PATH = Path.home() / ".cache" / "syncfield" / "quest_ip"

# mDNS service type the Quest companion app advertises. Matches the
# registerService() call in opengraph-studio's QuestMdnsAdvertiser.cs.
_MDNS_SERVICE_TYPE = "_syncfield-quest._tcp.local."


def _mdns_discover(timeout_s: float) -> Optional[str]:
    """Browse local mDNS for a Quest sender and return its IP.

    Relies on the Unity app's :file:`QuestMdnsAdvertiser.cs` component
    registering an ``_syncfield-quest._tcp`` service via Android
    ``NsdManager``. mDNS multicast crosses most home / office networks
    that filter UDP broadcasts (the protocol Apple uses for AirPlay /
    AirPrint) so this is the robust path — broadcast listen stays as
    a fallback for networks with multicast disabled too.

    Returns ``None`` when ``zeroconf`` isn't installed, no service
    appears within the timeout, or the resolver hands back an address
    that doesn't look like an IPv4.
    """
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        logger.info(
            "discover_quest_ip: zeroconf not installed — skipping mDNS. "
            "Install with `pip install syncfield[camera]` for mDNS support."
        )
        return None

    import threading as _threading

    found_event = _threading.Event()
    found_ip: Dict[str, Optional[str]] = {"ip": None}

    class _Listener(ServiceListener):
        def add_service(self, zc, type_, name):  # noqa: D401
            info = zc.get_service_info(type_, name, timeout=1500)
            if info is None or not info.addresses:
                return
            # addresses is a list of packed IPv4/IPv6; take the first
            # routable IPv4 we see.
            for raw in info.addresses:
                if len(raw) == 4:
                    ip = socket.inet_ntoa(raw)
                    logger.info("discover_quest_ip: mDNS found Quest at %s", ip)
                    found_ip["ip"] = ip
                    found_event.set()
                    return

        def update_service(self, zc, type_, name): pass
        def remove_service(self, zc, type_, name): pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, _MDNS_SERVICE_TYPE, _Listener())
        found_event.wait(timeout=timeout_s)
        return found_ip["ip"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("discover_quest_ip: mDNS error: %s", exc)
        return None
    finally:
        try:
            zc.close()
        except Exception:
            pass


def _probe_quest_alive(ip: str, timeout_s: float = 1.5) -> bool:
    """Cheap reachability check — does the Quest companion HTTP server
    answer at ``http://{ip}:14045/status``? Used to validate a cached
    or env-supplied address before committing to it.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://{ip}:{_QUEST_HTTP_PORT}/status", timeout=timeout_s,
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _read_cached_quest_ip() -> Optional[str]:
    try:
        return _QUEST_IP_CACHE_PATH.read_text().strip() or None
    except OSError:
        return None


def _write_cached_quest_ip(ip: str) -> None:
    try:
        _QUEST_IP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _QUEST_IP_CACHE_PATH.write_text(ip)
    except OSError:
        pass  # cache is best-effort; stale entries get overwritten next time


def discover_quest_ip(timeout_s: float = 8.0) -> Optional[str]:
    """Resolve the Quest's IPv4 via mDNS with layered fallbacks.

    Resolution order — first non-``None`` result wins:

    1. ``QUEST_IP`` env var — verbatim override for multi-Quest setups
       / CI / the rare network that filters both mDNS AND broadcast.
    2. **mDNS browse for ``_syncfield-quest._tcp``** (default ~3 s of
       the budget). The Quest companion app advertises this service
       on start via Android ``NsdManager``. Works on any network that
       passes multicast DNS (most home / office WiFi — same protocol
       AirPlay, AirPrint, Chromecast rely on).
    3. Cached IP from ``~/.cache/syncfield/quest_ip``, validated by a
       1.5 s ``/status`` probe. Stale caches after a DHCP lease
       change fall through naturally.
    4. UDP ``:14044`` broadcast listener — picks up the Quest's own
       ``SYNCFIELD_DISCOVER_RECORDER_V1`` probe. Last resort for
       networks that block mDNS but pass UDP broadcast.

    On success the result is cached so subsequent runs re-resolve in
    <2 s. Returns ``None`` when every path fails; caller should print
    an actionable error pointing at the env-var override.
    """
    import os

    env_ip = os.environ.get("QUEST_IP")
    if env_ip:
        logger.info("discover_quest_ip: using QUEST_IP env override = %s", env_ip)
        return env_ip

    # Cache first: on a stable network the Quest usually keeps the
    # same DHCP lease and an HTTP probe (~1.5 s worst case) is cheap.
    # mDNS, by contrast, takes ~5 s of wall-clock on macOS because
    # zeroconf shares port 5353 with mDNSResponder and needs a warm-up.
    cached = _read_cached_quest_ip()
    if cached and _probe_quest_alive(cached):
        logger.info("discover_quest_ip: cached IP %s is alive", cached)
        return cached
    if cached:
        logger.info(
            "discover_quest_ip: cached IP %s did not answer; falling back to mDNS",
            cached,
        )

    # Fresh network / DHCP renewal / first run: ask mDNS. The Quest
    # app advertises ``_syncfield-quest._tcp`` via Android NsdManager
    # so any subnet that passes multicast DNS returns the answer
    # within ~5 s.
    mdns_ip = _mdns_discover(6.0)
    if mdns_ip:
        _write_cached_quest_ip(mdns_ip)
        return mdns_ip

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", DEFAULT_DISCOVERY_PORT))
        except OSError as exc:
            logger.warning(
                "discover_quest_ip: cannot bind UDP :%d (%s) — set "
                "QUEST_IP env var to skip discovery.",
                DEFAULT_DISCOVERY_PORT, exc,
            )
            return None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            sock.settimeout(min(1.0, max(0.05, deadline - time.monotonic())))
            try:
                data, addr = sock.recvfrom(512)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return None
            if data.strip() == DISCOVERY_PROBE:
                logger.info("discover_quest_ip: found Quest at %s", addr[0])
                _write_cached_quest_ip(addr[0])
                return addr[0]
        logger.warning(
            "discover_quest_ip: no probe received in %.1fs — Quest app "
            "may not be running, or AP is dropping broadcasts. Set "
            "QUEST_IP env var to skip discovery.",
            timeout_s,
        )
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass

# OpenXR standard joint names (26 per hand)
JOINT_NAMES = [
    "Palm", "Wrist",
    "ThumbMetacarpal", "ThumbProximal", "ThumbDistal", "ThumbTip",
    "IndexMetacarpal", "IndexProximal", "IndexIntermediate", "IndexDistal", "IndexTip",
    "MiddleMetacarpal", "MiddleProximal", "MiddleIntermediate", "MiddleDistal", "MiddleTip",
    "RingMetacarpal", "RingProximal", "RingIntermediate", "RingDistal", "RingTip",
    "LittleMetacarpal", "LittleProximal", "LittleIntermediate", "LittleDistal", "LittleTip",
]

ALL_JOINT_NAMES: List[str] = []
for _side in ("Left", "Right"):
    for _joint in JOINT_NAMES:
        ALL_JOINT_NAMES.append(f"{_side}{_joint}")

# Finger chains for visualization (OpenXR 26-joint layout, 1-indexed from Wrist)
FINGER_CHAINS = [
    [1, 2, 3, 4, 5],           # Wrist → Thumb
    [1, 6, 7, 8, 9, 10],       # Wrist → Index
    [1, 11, 12, 13, 14, 15],   # Wrist → Middle
    [1, 16, 17, 18, 19, 20],   # Wrist → Ring
    [1, 21, 22, 23, 24, 25],   # Wrist → Little
]


# ---------------------------------------------------------------------------
# MetaQuestHandStream
# ---------------------------------------------------------------------------


class MetaQuestHandStream(StreamBase):
    """Meta Quest 3 hand/head tracker via WiFi UDP.

    Receives JSON packets from a Unity app running on Quest 3.
    Emits sensor samples with channels:

    - ``hand_joints``: 156 floats (26 joints x 3 xyz x 2 hands)
    - ``joint_rotations``: 208 floats (26 joints x 4 quat x 2 hands)
    - ``head_pose``: 7 floats (pos3 + quat4), or omitted if not available

    Args:
        id: Stream identifier.
        host: UDP bind address. Default ``"0.0.0.0"`` (all interfaces).
        port: UDP bind port. Default ``14043``.
        mode: ``"hand"`` (full skeleton) or ``"controller"``
            (controller pose mapped to wrist joint slots).
    """

    MAX_CONSECUTIVE_ERRORS = 5
    BUFFER_SIZE = 65536
    CONNECTION_TIMEOUT_S = 2.0

    # Samples arrive via WiFi UDP from a remote Quest device, so they do
    # not share the host monotonic clock domain. Tag them separately so
    # downstream sync tooling can account for the wireless jitter.
    CLOCK_DOMAIN = "remote_quest3"
    UNCERTAINTY_NS = 10_000_000  # 10 ms — typical WiFi jitter budget

    _discovery_kind = "sensor"
    _discovery_adapter_type = "meta_quest"

    # Quest companion app HTTP control port (same CameraHttpServer that
    # serves /status, /preview, /recording/*). We piggy-back on it to
    # push the Mac's IP directly to the tracker, skipping UDP broadcast
    # discovery entirely (which some APs silently drop).
    DEFAULT_QUEST_HTTP_PORT = 14045

    def __init__(
        self,
        id: str,
        *,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        mode: str = "hand",
        quest_host: Optional[str] = None,
        quest_http_port: int = 14045,
    ) -> None:
        super().__init__(
            id=id,
            kind="sensor",
            capabilities=StreamCapabilities(
                provides_audio_track=False,
                supports_precise_timestamps=True,
                is_removable=True,
                produces_file=False,
            ),
        )
        self._host = host
        self._port = port
        self._mode = mode.lower() if mode in ("hand", "controller") else "hand"
        self._quest_host = quest_host
        self._quest_http_port = quest_http_port

        self._socket: Optional[socket.socket] = None
        self._receive_thread: Optional[threading.Thread] = None
        self._discovery_socket: Optional[socket.socket] = None
        self._discovery_thread: Optional[threading.Thread] = None
        self._discovery_port = DEFAULT_DISCOVERY_PORT
        self._stop_event = threading.Event()
        self._recording = False
        self._frame_count = 0
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None
        self._consecutive_errors = 0
        self._last_packet_mono: float = 0.0
        # "waiting"  = socket is up, no packet has ever been received yet
        # "connected" = a packet arrived within CONNECTION_TIMEOUT_S
        # "lost"     = had packets, none for > CONNECTION_TIMEOUT_S (DROP emitted)
        self._connection_state: str = "waiting"

        # Start the discovery responder eagerly so the Quest can
        # auto-resolve our IP as soon as the adapter is instantiated —
        # before the user clicks "Connect" in the viewer. Without this
        # the Quest keeps sending to its stale default IP until the
        # 4-phase lifecycle's connect() fires.
        self._start_discovery_responder()

    # ------------------------------------------------------------------
    # 4-phase lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Bind the UDP socket and start receiving packets."""
        if self._receive_thread is not None and self._receive_thread.is_alive():
            return

        self._stop_event.clear()
        self._frame_count = 0
        self._first_at = None
        self._last_at = None
        self._consecutive_errors = 0
        self._last_packet_mono = 0.0
        self._connection_state = "waiting"

        self._create_socket()
        self._receive_thread = threading.Thread(
            target=self._receive_loop,
            name=f"quest3-{self.id}",
            daemon=True,
        )
        self._receive_thread.start()
        logger.info(
            "[%s] Quest 3 UDP receiver started on %s:%d (mode=%s)",
            self.id, self._host, self._port, self._mode,
        )

        # Direct-push of our IP to the Quest companion app — bypasses
        # UDP broadcast discovery, which some APs silently drop. Only
        # runs when the caller passed quest_host (i.e. they know where
        # the headset is); otherwise we fall back to the responder path.
        if self._quest_host:
            try:
                self._push_target_to_quest()
            except Exception as exc:
                logger.warning(
                    "[%s] Could not push target IP to Quest at %s:%d: %s "
                    "(falling back to broadcast discovery)",
                    self.id, self._quest_host, self._quest_http_port, exc,
                )

        # Spin up the discovery responder so the Quest companion app can
        # auto-resolve our IP. Without this the user has to type the
        # Mac's IP into the Quest HUD every time the network changes.
        self._start_discovery_responder()

    def start_recording(self, session_clock: SessionClock) -> None:
        self._recording = True
        self._frame_count = 0
        self._first_at = None
        self._last_at = None

    def stop_recording(self) -> FinalizationReport:
        self._recording = False
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=self._frame_count,
            file_path=None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=None,
        )

    def disconnect(self) -> None:
        """Stop the receive thread and close the socket."""
        self._stop_event.set()
        if self._receive_thread is not None:
            self._receive_thread.join(timeout=2.0)
            self._receive_thread = None
        if self._discovery_thread is not None:
            self._discovery_thread.join(timeout=2.0)
            self._discovery_thread = None
        if self._discovery_socket is not None:
            try:
                self._discovery_socket.close()
            except Exception:
                pass
            self._discovery_socket = None
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

    # ------------------------------------------------------------------
    # Discovery responder (UDP :14044)
    # ------------------------------------------------------------------

    def _push_target_to_quest(self) -> None:
        """POST our local IP to the Quest's CameraHttpServer.

        The Quest's UDPTrackingSender will then unicast tracking packets
        directly to us — no UDP broadcast required. This is the reliable
        path when the Wi-Fi AP drops broadcasts (client isolation,
        IPv6-only, etc.).

        Uses urllib so we don't pull httpx into the base sensor adapter
        just for one request — the camera adapter has its own httpx
        dependency, but MetaQuestHandStream stays import-light.
        """
        import urllib.request

        local_ip = self._resolve_local_ip_for(self._quest_host)
        payload = json.dumps({"ip": local_ip, "port": self._port}).encode("utf-8")
        url = f"http://{self._quest_host}:{self._quest_http_port}/tracker/target"
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            logger.info(
                "[%s] Pushed target %s:%d to Quest %s — %s",
                self.id, local_ip, self._port, self._quest_host, body.strip(),
            )

    def _start_discovery_responder(self) -> None:
        """Respond to Quest companion-app discovery broadcasts.

        The Quest sender broadcasts :data:`DISCOVERY_PROBE` every ~2 s
        on :data:`DEFAULT_DISCOVERY_PORT` until a recorder replies.
        We listen on that port and reply with a JSON payload naming
        ourselves as the tracker target — Quest then locks onto our
        IP automatically and starts sending on :data:`DEFAULT_PORT`.

        Multiple ``MetaQuestHandStream`` instances in one process would
        fight over :14044, so we silently skip the bind if another
        responder already owns the port. Single-Quest setups — the
        common case — just work.
        """
        if self._discovery_thread is not None and self._discovery_thread.is_alive():
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self._discovery_port))
            sock.settimeout(1.0)
        except OSError as exc:
            logger.info(
                "[%s] Discovery responder disabled (port %d busy: %s). "
                "If your Quest can't auto-find this Mac, set the receiver "
                "IP manually in the Quest app HUD.",
                self.id, self._discovery_port, exc,
            )
            return

        self._discovery_socket = sock
        self._discovery_thread = threading.Thread(
            target=self._discovery_loop,
            name=f"quest3-discovery-{self.id}",
            daemon=True,
        )
        self._discovery_thread.start()
        logger.info(
            "[%s] Quest 3 discovery responder listening on :%d",
            self.id, self._discovery_port,
        )

    def _discovery_loop(self) -> None:
        while not self._stop_event.is_set():
            sock = self._discovery_socket
            if sock is None:
                break
            try:
                data, addr = sock.recvfrom(512)
            except TimeoutError:
                continue
            except Exception:
                if self._stop_event.is_set():
                    return
                continue
            if data.strip() != DISCOVERY_PROBE:
                continue
            response = self._build_discovery_response(addr[0])
            try:
                sock.sendto(response, addr)
                logger.info(
                    "[%s] Answered Quest discovery probe from %s",
                    self.id, addr[0],
                )
            except Exception as exc:
                logger.warning("[%s] Discovery reply failed: %s", self.id, exc)

    def _build_discovery_response(self, quest_ip: str) -> bytes:
        """Build the JSON response shape the Unity app's
        ``RecorderDiscoveryResponse`` parser expects.

        We source our own IP by opening a UDP socket toward the Quest
        and reading the resulting local endpoint — that's the address
        we'd send out of, which is the address the Quest should target.
        """
        recorder_ip = self._resolve_local_ip_for(quest_ip)
        payload = {
            "type": DISCOVERY_RESPONSE_TYPE,
            "recorder_ip": recorder_ip,
            "tracker_port": self._port,
            "api_port": 0,
            "hostname": socket.gethostname(),
            "label": "SyncField (Python)",
            "active_config": "",
            "ts_ms": int(time.time() * 1000),
        }
        return json.dumps(payload).encode("utf-8")

    @staticmethod
    def _resolve_local_ip_for(remote_ip: str) -> str:
        """Return the local IPv4 the OS would use to reach ``remote_ip``.

        Creating a UDP socket and calling ``connect()`` doesn't send any
        packets; it just makes the kernel pick the right outbound
        interface. Reading ``getsockname`` after that gives the IP the
        Quest should target.
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((remote_ip, 1))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            try:
                s.close()
            except Exception:
                pass

    # Legacy one-shot compatibility
    def prepare(self) -> None:
        pass

    def start(self, session_clock: SessionClock) -> None:  # type: ignore[override]
        self.connect()
        self.start_recording(session_clock)

    def stop(self) -> FinalizationReport:
        report = self.stop_recording()
        self.disconnect()
        return report

    # ------------------------------------------------------------------
    # UDP socket management
    # ------------------------------------------------------------------

    def _create_socket(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                pass

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_RCVBUF, self.BUFFER_SIZE * 4,
            )
        except Exception:
            pass
        self._socket.bind((self._host, self._port))
        self._socket.settimeout(1.0)

    @property
    def is_connected(self) -> bool:
        """True when a packet has arrived within ``CONNECTION_TIMEOUT_S``.

        Mirrors the recorder's ``is_connected`` semantics: the socket
        being open is not enough — the Quest must actually be streaming.
        Callers can poll this from any thread; it reads a float that
        CPython updates atomically.
        """
        if self._last_packet_mono == 0.0:
            return False
        return (
            time.monotonic() - self._last_packet_mono
            <= self.CONNECTION_TIMEOUT_S
        )

    def _receive_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._socket is None:
                break
            try:
                data, _ = self._socket.recvfrom(self.BUFFER_SIZE)
                self._process_packet(data)
            except TimeoutError:
                # The 1-second recv timeout doubles as a watchdog tick —
                # use it to notice when the Quest has stopped streaming.
                self._check_connection_timeout()
                continue
            except Exception as exc:
                if not self._stop_event.is_set():
                    self._handle_socket_error(exc)

    def _check_connection_timeout(self) -> None:
        """Emit DROP if we had packets and now haven't seen one for > timeout."""
        if self._connection_state != "connected":
            return
        if self._last_packet_mono == 0.0:
            return
        silence_s = time.monotonic() - self._last_packet_mono
        if silence_s > self.CONNECTION_TIMEOUT_S:
            self._connection_state = "lost"
            self._emit_health(HealthEvent(
                self.id, HealthEventKind.DROP,
                time.monotonic_ns(),
                f"No packet for {silence_s:.1f}s (timeout {self.CONNECTION_TIMEOUT_S}s)",
            ))

    def _handle_socket_error(self, error: Exception) -> None:
        self._consecutive_errors += 1
        if self._consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
            logger.info("[%s] Attempting reconnection...", self.id)
            try:
                self._create_socket()
                self._consecutive_errors = 0
            except Exception:
                time.sleep(1.0)
        self._emit_health(HealthEvent(
            self.id, HealthEventKind.WARNING,
            time.monotonic_ns(), f"Socket error: {error}",
        ))

    # ------------------------------------------------------------------
    # Packet processing
    # ------------------------------------------------------------------

    def _process_packet(self, data: bytes) -> None:
        capture_ns = time.monotonic_ns()
        try:
            packet = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._emit_health(HealthEvent(
                self.id, HealthEventKind.WARNING,
                capture_ns, "JSON decode error",
            ))
            return

        self._last_packet_mono = time.monotonic()
        self._consecutive_errors = 0
        self._update_connection_state_on_packet(capture_ns)

        # Build channels from packet
        channels = self._parse_channels(packet)

        # Always emit so the viewer's live panel shows tracking data the
        # moment the headset is connected — without this the user has to
        # press Record before they can confirm the sensor is even alive.
        # Recording state still gates first/last_at (those describe the
        # recorded segment, not the live preview) and the FinalizationReport
        # frame count.
        frame_number = self._frame_count
        self._frame_count += 1
        if self._recording:
            if self._first_at is None:
                self._first_at = capture_ns
            self._last_at = capture_ns
        self._emit_sample(SampleEvent(
            stream_id=self.id,
            frame_number=frame_number,
            capture_ns=capture_ns,
            channels=channels,
            uncertainty_ns=self.UNCERTAINTY_NS,
            clock_domain=self.CLOCK_DOMAIN,
        ))

    def _update_connection_state_on_packet(self, at_ns: int) -> None:
        """Emit HEARTBEAT on first packet, RECONNECT after a drop."""
        prev = self._connection_state
        if prev == "connected":
            return
        self._connection_state = "connected"
        if prev == "waiting":
            self._emit_health(HealthEvent(
                self.id, HealthEventKind.HEARTBEAT,
                at_ns, "First Quest 3 packet received",
            ))
        elif prev == "lost":
            self._emit_health(HealthEvent(
                self.id, HealthEventKind.RECONNECT,
                at_ns, "Quest 3 packet stream resumed",
            ))

    def _parse_channels(self, packet: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a Quest 3 JSON packet into flat channel dict."""
        channels: Dict[str, Any] = {
            "hand_joints": self._extract_hand_joints(packet),
            "joint_rotations": self._extract_joint_rotations(packet),
        }
        head_pose = self._extract_head_pose(packet)
        if head_pose is not None:
            channels["head_pose"] = head_pose
        return channels

    # ------------------------------------------------------------------
    # Joint extraction (hand mode)
    # ------------------------------------------------------------------

    def _extract_hand_joints(self, packet: Dict[str, Any]) -> List[float]:
        """Extract joint positions: 156 floats (26 joints x 3 xyz x 2 hands)."""
        if self._mode == "controller":
            return self._extract_controller_wrist_joints(packet)

        joints: List[float] = []
        for side in ("left", "right"):
            hand = packet.get(side, {})
            if hand.get("tracked", False) and "joints" in hand:
                for i in range(NUM_JOINTS):
                    if i < len(hand["joints"]):
                        pos = hand["joints"][i].get("pos", [0.0, 0.0, 0.0])
                        joints.extend(float(v) for v in pos[:3])
                    else:
                        joints.extend([0.0, 0.0, 0.0])
            else:
                joints.extend([0.0] * (NUM_JOINTS * NUM_COORDS))
        return joints

    def _extract_joint_rotations(self, packet: Dict[str, Any]) -> List[float]:
        """Extract joint rotations: 208 floats (26 joints x 4 quat x 2 hands)."""
        if self._mode == "controller":
            return self._extract_controller_wrist_rotations(packet)

        rotations: List[float] = []
        for side in ("left", "right"):
            hand = packet.get(side, {})
            if hand.get("tracked", False) and "joints" in hand:
                for i in range(NUM_JOINTS):
                    if i < len(hand["joints"]):
                        rot = hand["joints"][i].get("rot", [0.0, 0.0, 0.0, 1.0])
                        rotations.extend(float(v) for v in rot[:4])
                    else:
                        rotations.extend([0.0, 0.0, 0.0, 1.0])
            else:
                for _ in range(NUM_JOINTS):
                    rotations.extend([0.0, 0.0, 0.0, 1.0])
        return rotations

    def _extract_head_pose(self, packet: Dict[str, Any]) -> Optional[List[float]]:
        """Extract head pose: 7 floats [x, y, z, qx, qy, qz, qw]."""
        head = packet.get("head")
        if head is None:
            return None
        pos = head.get("pos", [0.0, 0.0, 0.0])
        rot = head.get("rot", [0.0, 0.0, 0.0, 1.0])
        return [float(v) for v in pos[:3]] + [float(v) for v in rot[:4]]

    # ------------------------------------------------------------------
    # Controller mode — map controller pose to wrist joint slots
    # ------------------------------------------------------------------

    def _extract_controller_wrist_joints(self, packet: Dict[str, Any]) -> List[float]:
        joints: List[float] = []
        for side in ("left", "right"):
            side_joints = [0.0] * (NUM_JOINTS * NUM_COORDS)
            ctrl = self._controller_data(packet, side)
            if ctrl.get("tracked", False):
                pos = ctrl.get("pos", [0.0, 0.0, 0.0])
                base = WRIST_JOINT_INDEX * NUM_COORDS
                side_joints[base:base + NUM_COORDS] = [float(v) for v in pos[:3]]
            joints.extend(side_joints)
        return joints

    def _extract_controller_wrist_rotations(self, packet: Dict[str, Any]) -> List[float]:
        rotations: List[float] = []
        for side in ("left", "right"):
            ctrl = self._controller_data(packet, side)
            wrist_quat = [0.0, 0.0, 0.0, 1.0]
            if ctrl.get("tracked", False):
                wrist_quat = [float(v) for v in ctrl.get("rot", [0.0, 0.0, 0.0, 1.0])[:4]]
            for joint_idx in range(NUM_JOINTS):
                if joint_idx == WRIST_JOINT_INDEX:
                    rotations.extend(wrist_quat)
                else:
                    rotations.extend([0.0, 0.0, 0.0, 1.0])
        return rotations

    @staticmethod
    def _controller_data(packet: Dict[str, Any], side: str) -> Dict[str, Any]:
        controllers = packet.get("controllers", {})
        ctrl = controllers.get(side, {})
        return ctrl if isinstance(ctrl, dict) else {}
