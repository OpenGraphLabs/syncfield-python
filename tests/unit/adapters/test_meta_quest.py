"""Tests for the MetaQuestHandStream adapter.

Ported from opengraph-studio/recorder tests/test_quest3_tracker.py
and adapted to the syncfield StreamBase 4-phase lifecycle.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from syncfield.adapters.meta_quest import (
    ALL_JOINT_NAMES,
    DEFAULT_PORT,
    HEAD_POSE_DIM,
    JOINTS_DIM,
    MetaQuestHandStream,
    ROTATIONS_DIM,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_quest3_packet(
    *,
    left_tracked: bool = True,
    right_tracked: bool = True,
    include_head: bool = True,
    include_controllers: bool = False,
    left_controller_tracked: bool = False,
    right_controller_tracked: bool = False,
) -> dict:
    """Build a realistic Quest 3 UDP packet."""

    def _make_joints(offset: float = 0.0) -> list:
        return [
            {"pos": [i * 0.01 + offset, i * 0.02, i * 0.03], "rot": [0.0, 0.0, 0.0, 1.0]}
            for i in range(26)
        ]

    packet: dict = {
        "v": 1,
        "seq": 42,
        "ts_ms": time.time() * 1000,
    }
    if include_head:
        packet["head"] = {"pos": [0.0, 1.7, 0.0], "rot": [0.0, 0.0, 0.0, 1.0]}

    packet["left"] = {"tracked": left_tracked, "joints": _make_joints(0.0) if left_tracked else []}
    packet["right"] = {"tracked": right_tracked, "joints": _make_joints(1.0) if right_tracked else []}

    if include_controllers:
        packet["controllers"] = {
            "left": {
                "tracked": left_controller_tracked,
                "pos": [0.1, 0.2, 0.3],
                "rot": [0.0, 0.0, 0.707, 0.707],
            },
            "right": {
                "tracked": right_controller_tracked,
                "pos": [0.4, 0.5, 0.6],
                "rot": [0.0, 0.0, -0.707, 0.707],
            },
        }

    return packet


def _send_packet(port: int, packet: dict) -> None:
    """Send a JSON packet via UDP to localhost."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(json.dumps(packet).encode(), ("127.0.0.1", port))
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Schema and dimensions
# ---------------------------------------------------------------------------


class TestSchemaAndDimensions:
    def test_joints_dim(self):
        assert JOINTS_DIM == 156  # 26 x 3 x 2

    def test_rotations_dim(self):
        assert ROTATIONS_DIM == 208  # 26 x 4 x 2

    def test_head_pose_dim(self):
        assert HEAD_POSE_DIM == 7  # pos3 + quat4

    def test_joint_names_count(self):
        assert len(ALL_JOINT_NAMES) == 52  # 26 x 2 hands

    def test_joint_names_sides(self):
        left_names = [n for n in ALL_JOINT_NAMES if n.startswith("Left")]
        right_names = [n for n in ALL_JOINT_NAMES if n.startswith("Right")]
        assert len(left_names) == 26
        assert len(right_names) == 26

    def test_default_port(self):
        assert DEFAULT_PORT == 14043


# ---------------------------------------------------------------------------
# Joint extraction (pure logic, no network)
# ---------------------------------------------------------------------------


class TestHandJointExtraction:
    def test_both_hands_tracked(self):
        stream = MetaQuestHandStream("test", port=0)
        packet = _make_quest3_packet()
        joints = stream._extract_hand_joints(packet)
        assert len(joints) == 156
        # Left hand should have non-zero values
        assert any(v != 0.0 for v in joints[:78])
        # Right hand should have non-zero values
        assert any(v != 0.0 for v in joints[78:])

    def test_left_untracked(self):
        stream = MetaQuestHandStream("test", port=0)
        packet = _make_quest3_packet(left_tracked=False)
        joints = stream._extract_hand_joints(packet)
        assert len(joints) == 156
        # Left hand all zeros
        assert all(v == 0.0 for v in joints[:78])
        # Right hand has data
        assert any(v != 0.0 for v in joints[78:])

    def test_right_untracked(self):
        stream = MetaQuestHandStream("test", port=0)
        packet = _make_quest3_packet(right_tracked=False)
        joints = stream._extract_hand_joints(packet)
        assert len(joints) == 156
        assert any(v != 0.0 for v in joints[:78])
        assert all(v == 0.0 for v in joints[78:])


class TestJointRotationExtraction:
    def test_rotation_dim(self):
        stream = MetaQuestHandStream("test", port=0)
        packet = _make_quest3_packet()
        rotations = stream._extract_joint_rotations(packet)
        assert len(rotations) == 208

    def test_untracked_fills_identity(self):
        stream = MetaQuestHandStream("test", port=0)
        packet = _make_quest3_packet(left_tracked=False)
        rotations = stream._extract_joint_rotations(packet)
        # Left hand: all identity quaternions [0,0,0,1]
        for i in range(26):
            base = i * 4
            assert rotations[base:base + 4] == [0.0, 0.0, 0.0, 1.0]


class TestHeadPoseExtraction:
    def test_head_pose(self):
        stream = MetaQuestHandStream("test", port=0)
        packet = _make_quest3_packet(include_head=True)
        pose = stream._extract_head_pose(packet)
        assert pose is not None
        assert len(pose) == 7
        assert pose[:3] == [0.0, 1.7, 0.0]  # position
        assert pose[3:] == [0.0, 0.0, 0.0, 1.0]  # quaternion

    def test_no_head(self):
        stream = MetaQuestHandStream("test", port=0)
        packet = _make_quest3_packet(include_head=False)
        pose = stream._extract_head_pose(packet)
        assert pose is None


# ---------------------------------------------------------------------------
# Controller mode
# ---------------------------------------------------------------------------


class TestControllerMode:
    def test_controller_wrist_joints(self):
        stream = MetaQuestHandStream("test", port=0, mode="controller")
        packet = _make_quest3_packet(
            include_controllers=True,
            left_controller_tracked=True,
            right_controller_tracked=True,
        )
        joints = stream._extract_hand_joints(packet)
        assert len(joints) == 156
        # Left wrist (joint index 1, coords 3-5)
        assert joints[3] == pytest.approx(0.1)
        assert joints[4] == pytest.approx(0.2)
        assert joints[5] == pytest.approx(0.3)
        # Right wrist (joint index 1 in right hand, offset 78 + 3)
        assert joints[81] == pytest.approx(0.4)

    def test_untracked_controller_zeros(self):
        stream = MetaQuestHandStream("test", port=0, mode="controller")
        packet = _make_quest3_packet(
            include_controllers=True,
            left_controller_tracked=False,
            right_controller_tracked=False,
        )
        joints = stream._extract_hand_joints(packet)
        assert all(v == 0.0 for v in joints)


# ---------------------------------------------------------------------------
# Channel parsing
# ---------------------------------------------------------------------------


class TestChannelParsing:
    def test_channels_include_all_fields(self):
        stream = MetaQuestHandStream("test", port=0)
        packet = _make_quest3_packet()
        channels = stream._parse_channels(packet)
        assert "hand_joints" in channels
        assert "joint_rotations" in channels
        assert "head_pose" in channels
        assert len(channels["hand_joints"]) == 156
        assert len(channels["joint_rotations"]) == 208
        assert len(channels["head_pose"]) == 7

    def test_channels_no_head(self):
        stream = MetaQuestHandStream("test", port=0)
        packet = _make_quest3_packet(include_head=False)
        channels = stream._parse_channels(packet)
        assert "head_pose" not in channels


# ---------------------------------------------------------------------------
# Lifecycle (with real UDP)
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_connect_disconnect(self):
        stream = MetaQuestHandStream("test", port=0)
        # port=0 means we can't actually bind, but let's test with a real port
        port = _find_free_port()
        stream._port = port
        stream.connect()
        assert stream._receive_thread is not None
        assert stream._receive_thread.is_alive()
        stream.disconnect()
        assert stream._socket is None

    def test_recording_lifecycle(self):
        from syncfield.clock import SessionClock, SyncPoint

        port = _find_free_port()
        stream = MetaQuestHandStream("test", port=port)
        stream.connect()

        clock = SessionClock(sync_point=SyncPoint.create_now("test"))
        stream.start_recording(clock)

        # Send a packet
        _send_packet(port, _make_quest3_packet())
        time.sleep(0.2)

        report = stream.stop_recording()
        stream.disconnect()

        assert report.status == "completed"
        assert report.stream_id == "test"
        assert report.frame_count >= 1

    def test_samples_emitted_during_recording(self):
        from syncfield.clock import SessionClock, SyncPoint

        port = _find_free_port()
        stream = MetaQuestHandStream("test", port=port)

        received = []
        stream.on_sample(lambda e: received.append(e))

        stream.connect()
        clock = SessionClock(sync_point=SyncPoint.create_now("test"))
        stream.start_recording(clock)

        _send_packet(port, _make_quest3_packet())
        time.sleep(0.2)

        stream.stop_recording()
        stream.disconnect()

        assert len(received) >= 1
        event = received[0]
        assert event.stream_id == "test"
        assert "hand_joints" in event.channels
        assert len(event.channels["hand_joints"]) == 156


# ---------------------------------------------------------------------------
# Clock metadata (clock_domain + uncertainty_ns) emitted on SampleEvent
# ---------------------------------------------------------------------------


class TestClockMetadata:
    def test_sample_event_has_remote_quest_clock_domain(self):
        from syncfield.clock import SessionClock, SyncPoint

        port = _find_free_port()
        stream = MetaQuestHandStream("test", port=port)
        received = []
        stream.on_sample(lambda e: received.append(e))

        stream.connect()
        stream.start_recording(SessionClock(sync_point=SyncPoint.create_now("h")))
        _send_packet(port, _make_quest3_packet())
        time.sleep(0.2)
        stream.stop_recording()
        stream.disconnect()

        assert received, "expected at least one sample"
        assert received[0].clock_domain == "remote_quest3"
        assert received[0].uncertainty_ns == 10_000_000


# ---------------------------------------------------------------------------
# Connection health — is_connected property + DROP/HEARTBEAT/RECONNECT
# ---------------------------------------------------------------------------


class TestConnectionHealth:
    def test_is_connected_false_before_any_packet(self):
        stream = MetaQuestHandStream("test", port=0)
        assert stream.is_connected is False

    def test_is_connected_true_after_recent_packet(self):
        port = _find_free_port()
        stream = MetaQuestHandStream("test", port=port)
        stream.connect()
        try:
            _send_packet(port, _make_quest3_packet())
            time.sleep(0.2)
            assert stream.is_connected is True
        finally:
            stream.disconnect()

    def test_heartbeat_emitted_on_first_packet(self):
        port = _find_free_port()
        stream = MetaQuestHandStream("test", port=port)
        events = []
        stream.on_health(lambda e: events.append(e))

        stream.connect()
        try:
            _send_packet(port, _make_quest3_packet())
            time.sleep(0.2)
        finally:
            stream.disconnect()

        kinds = [e.kind.value for e in events]
        assert "heartbeat" in kinds

    def test_drop_emitted_after_silence(self):
        # Shorten timeout so the watchdog fires quickly in tests.
        port = _find_free_port()
        stream = MetaQuestHandStream("test", port=port)
        stream.CONNECTION_TIMEOUT_S = 0.3  # override at instance level
        events = []
        stream.on_health(lambda e: events.append(e))

        stream.connect()
        try:
            # Mark the stream connected, then let the receive loop's
            # 1-second socket timeout fire once with no further packets.
            _send_packet(port, _make_quest3_packet())
            time.sleep(0.2)
            assert stream.is_connected is True
            # Wait long enough for silence to exceed CONNECTION_TIMEOUT_S
            # and for at least one recv timeout tick (socket timeout = 1.0s).
            time.sleep(1.5)
        finally:
            stream.disconnect()

        kinds = [e.kind.value for e in events]
        assert "drop" in kinds, f"expected drop in {kinds}"

    def test_reconnect_emitted_after_drop_resumes(self):
        port = _find_free_port()
        stream = MetaQuestHandStream("test", port=port)
        stream.CONNECTION_TIMEOUT_S = 0.3
        events = []
        stream.on_health(lambda e: events.append(e))

        stream.connect()
        try:
            _send_packet(port, _make_quest3_packet())
            time.sleep(0.2)
            time.sleep(1.5)  # allow drop to fire
            _send_packet(port, _make_quest3_packet())
            time.sleep(0.2)
        finally:
            stream.disconnect()

        kinds = [e.kind.value for e in events]
        assert "drop" in kinds
        assert "reconnect" in kinds
        assert kinds.index("reconnect") > kinds.index("drop")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Intra-host sync anchor
# ---------------------------------------------------------------------------


class TestRecordingAnchor:
    """Per-recording-window intra-host sync anchor capture.

    MetaQuestHandStream receives packets via WiFi UDP — the current
    parser doesn't surface the Quest's device-side ``ts_ms`` as a
    separate scalar, so ``first_frame_device_ns`` is expected to stay
    ``None``. ``armed_host_ns`` and ``first_frame_host_ns`` are
    populated on the first packet that arrives after
    ``start_recording``.
    """

    def test_meta_quest_anchor_captured_without_device_ts(self):
        from syncfield.clock import SessionClock, SyncPoint

        port = _find_free_port()
        stream = MetaQuestHandStream("quest3", port=port)
        stream.connect()
        armed_ns = time.monotonic_ns()
        clock = SessionClock(
            sync_point=SyncPoint.create_now("h"),
            recording_armed_ns=armed_ns,
        )
        stream.start_recording(clock)

        _send_packet(port, _make_quest3_packet())
        time.sleep(0.2)

        report = stream.stop_recording()
        stream.disconnect()

        assert report.recording_anchor is not None
        assert report.recording_anchor.armed_host_ns == armed_ns
        assert report.recording_anchor.first_frame_host_ns >= armed_ns
        # KEY: Quest adapter doesn't surface device clock — stays None.
        assert report.recording_anchor.first_frame_device_ns is None
