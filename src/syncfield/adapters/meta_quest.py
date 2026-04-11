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

    _discovery_kind = "sensor"
    _discovery_adapter_type = "meta_quest"

    def __init__(
        self,
        id: str,
        *,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        mode: str = "hand",
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

        self._socket: Optional[socket.socket] = None
        self._receive_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._recording = False
        self._frame_count = 0
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None
        self._consecutive_errors = 0
        self._last_packet_mono = 0.0

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
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

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

    def _receive_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._socket is None:
                break
            try:
                data, _ = self._socket.recvfrom(self.BUFFER_SIZE)
                self._process_packet(data)
            except TimeoutError:
                continue
            except Exception as exc:
                if not self._stop_event.is_set():
                    self._handle_socket_error(exc)

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

        # Build channels from packet
        channels = self._parse_channels(packet)

        if self._recording:
            if self._first_at is None:
                self._first_at = capture_ns
            self._last_at = capture_ns
            frame_number = self._frame_count
            self._frame_count += 1
            self._emit_sample(SampleEvent(
                stream_id=self.id,
                frame_number=frame_number,
                capture_ns=capture_ns,
                channels=channels,
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
