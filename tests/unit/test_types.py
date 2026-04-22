"""Tests for syncfield.types."""

from syncfield.types import FrameTimestamp, SensorSample, SyncPoint


def test_sync_point_create_now():
    sp = SyncPoint.create_now("test_host")
    assert sp.host_id == "test_host"
    assert sp.monotonic_ns > 0
    assert sp.wall_clock_ns > 0
    assert sp.timestamp_ms > 0
    assert len(sp.iso_datetime) > 0


def test_sync_point_to_dict():
    sp = SyncPoint.create_now("h1")
    d = sp.to_dict()
    assert d["host_id"] == "h1"
    assert set(d.keys()) == {
        "monotonic_ns", "wall_clock_ns", "host_id", "timestamp_ms", "iso_datetime",
    }


def test_frame_timestamp_defaults():
    ts = FrameTimestamp(frame_number=0, capture_ns=123456789)
    assert ts.clock_source == "host_monotonic"
    assert ts.clock_domain == "local_host"
    assert ts.uncertainty_ns == 5_000_000


def test_frame_timestamp_to_dict():
    ts = FrameTimestamp(frame_number=5, capture_ns=999, clock_domain="rig_01")
    d = ts.to_dict()
    assert d == {
        "frame_number": 5,
        "capture_ns": 999,
        "clock_source": "host_monotonic",
        "clock_domain": "rig_01",
        "uncertainty_ns": 5_000_000,
    }


def test_frame_timestamp_round_trip():
    original = FrameTimestamp(
        frame_number=10,
        capture_ns=1234567890,
        clock_domain="my_host",
        uncertainty_ns=1_000_000,
    )
    restored = FrameTimestamp.from_dict(original.to_dict())
    assert restored.frame_number == original.frame_number
    assert restored.capture_ns == original.capture_ns
    assert restored.clock_domain == original.clock_domain
    assert restored.uncertainty_ns == original.uncertainty_ns


def test_frame_timestamp_from_dict_defaults():
    """from_dict should fill defaults for optional fields."""
    minimal = {"frame_number": 0, "capture_ns": 100}
    ts = FrameTimestamp.from_dict(minimal)
    assert ts.clock_source == "host_monotonic"
    assert ts.clock_domain == "local_host"
    assert ts.uncertainty_ns == 5_000_000


# --- SensorSample tests ---


def test_sensor_sample_defaults():
    sample = SensorSample(frame_number=0, capture_ns=123456789, channels={"x": 1.0})
    assert sample.clock_source == "host_monotonic"
    assert sample.clock_domain == "local_host"
    assert sample.uncertainty_ns == 5_000_000


def test_sensor_sample_to_dict():
    sample = SensorSample(
        frame_number=3,
        capture_ns=999,
        channels={"accel_x": 0.12, "accel_y": -0.34},
        clock_domain="rig_01",
    )
    d = sample.to_dict()
    assert d == {
        "frame_number": 3,
        "capture_ns": 999,
        "clock_source": "host_monotonic",
        "clock_domain": "rig_01",
        "uncertainty_ns": 5_000_000,
        "channels": {"accel_x": 0.12, "accel_y": -0.34},
    }


def test_sensor_sample_round_trip():
    original = SensorSample(
        frame_number=7,
        capture_ns=5555555,
        channels={"temp": 22.5, "humidity": 45.0},
        clock_domain="my_host",
        uncertainty_ns=1_000_000,
    )
    restored = SensorSample.from_dict(original.to_dict())
    assert restored.frame_number == original.frame_number
    assert restored.capture_ns == original.capture_ns
    assert restored.channels == original.channels
    assert restored.clock_source == original.clock_source
    assert restored.clock_domain == original.clock_domain
    assert restored.uncertainty_ns == original.uncertainty_ns


def test_sensor_sample_from_dict_defaults():
    """from_dict should fill defaults for optional fields; frame_number defaults to 0."""
    minimal = {"capture_ns": 100, "channels": {"v": 3.14}}
    sample = SensorSample.from_dict(minimal)
    assert sample.frame_number == 0
    assert sample.capture_ns == 100
    assert sample.channels == {"v": 3.14}
    assert sample.clock_source == "host_monotonic"
    assert sample.clock_domain == "local_host"
    assert sample.uncertainty_ns == 5_000_000


def test_sensor_sample_nested_round_trip():
    """SensorSample round-trips nested channel data through to_dict/from_dict."""
    channels = {
        "joints": {"wrist": [0.1, 0.2, 0.3], "elbow": [1.0, 2.0, 3.0]},
        "gestures": {"pinch": 0.95},
        "finger_angles": [12.5, 45.0, 30.0],
    }
    original = SensorSample(frame_number=0, capture_ns=100, channels=channels)
    restored = SensorSample.from_dict(original.to_dict())
    assert restored.channels == channels
    assert restored.channels["joints"]["wrist"] == [0.1, 0.2, 0.3]
    assert restored.channels["gestures"]["pinch"] == 0.95


import pytest
from dataclasses import FrozenInstanceError
from pathlib import Path

from syncfield.types import (
    ChirpSpec,
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    SessionReport,
    SessionState,
    StreamCapabilities,
)


class TestStreamCapabilities:
    def test_is_frozen(self):
        caps = StreamCapabilities(
            provides_audio_track=True,
            supports_precise_timestamps=True,
            is_removable=False,
            produces_file=True,
        )
        with pytest.raises(FrozenInstanceError):
            caps.provides_audio_track = False  # type: ignore[misc]

    def test_default_all_false(self):
        caps = StreamCapabilities()
        assert caps.provides_audio_track is False
        assert caps.supports_precise_timestamps is False
        assert caps.is_removable is False
        assert caps.produces_file is False
        assert caps.live_preview is True

    def test_to_dict_round_trip(self):
        caps = StreamCapabilities(
            provides_audio_track=True,
            supports_precise_timestamps=False,
            is_removable=True,
            produces_file=True,
        )
        d = caps.to_dict()
        assert d == {
            "provides_audio_track": True,
            "supports_precise_timestamps": False,
            "is_removable": True,
            "produces_file": True,
            "live_preview": True,
        }


class TestSessionState:
    def test_states(self):
        assert SessionState.IDLE.value == "idle"
        assert SessionState.PREPARING.value == "preparing"
        assert SessionState.RECORDING.value == "recording"
        assert SessionState.STOPPING.value == "stopping"
        assert SessionState.STOPPED.value == "stopped"


class TestHealthEvent:
    def test_fields(self):
        ev = HealthEvent(
            stream_id="cam_left",
            kind=HealthEventKind.HEARTBEAT,
            at_ns=123_456_789,
            detail=None,
        )
        assert ev.stream_id == "cam_left"
        assert ev.kind is HealthEventKind.HEARTBEAT
        assert ev.at_ns == 123_456_789
        assert ev.detail is None

    def test_to_dict(self):
        ev = HealthEvent(
            stream_id="imu",
            kind=HealthEventKind.DROP,
            at_ns=42,
            detail="buffer overflow",
        )
        assert ev.to_dict() == {
            "stream_id": "imu",
            "kind": "drop",
            "at_ns": 42,
            "detail": "buffer overflow",
            "severity": "info",
            "source": "unknown",
            "fingerprint": "",
            "data": {},
        }


class TestSampleEvent:
    def test_minimal(self):
        ev = SampleEvent(stream_id="cam", frame_number=7, capture_ns=1000)
        assert ev.stream_id == "cam"
        assert ev.frame_number == 7
        assert ev.capture_ns == 1000


class TestFinalizationReport:
    def test_completed(self):
        report = FinalizationReport(
            stream_id="cam_left",
            status="completed",
            frame_count=120,
            file_path=Path("/tmp/cam_left.mp4"),
            first_sample_at_ns=1000,
            last_sample_at_ns=5000,
            health_events=[],
            error=None,
        )
        assert report.status == "completed"
        assert report.error is None

    def test_failed_has_error(self):
        report = FinalizationReport(
            stream_id="broken",
            status="failed",
            frame_count=0,
            file_path=None,
            first_sample_at_ns=None,
            last_sample_at_ns=None,
            health_events=[],
            error="device disconnected",
        )
        assert report.status == "failed"
        assert report.error == "device disconnected"


class TestChirpSpec:
    def test_is_frozen(self):
        spec = ChirpSpec(from_hz=400, to_hz=2500, duration_ms=500, amplitude=0.8, envelope_ms=15)
        with pytest.raises(FrozenInstanceError):
            spec.from_hz = 100  # type: ignore[misc]

    def test_to_dict(self):
        spec = ChirpSpec(400, 2500, 500, 0.8, 15)
        assert spec.to_dict() == {
            "from_hz": 400,
            "to_hz": 2500,
            "duration_ms": 500,
            "amplitude": 0.8,
            "envelope_ms": 15,
        }


class TestSessionReport:
    def test_minimal(self):
        report = SessionReport(
            host_id="rig_01",
            finalizations=[],
            chirp_start_ns=None,
            chirp_stop_ns=None,
        )
        assert report.host_id == "rig_01"
        assert report.finalizations == []


from syncfield.health.severity import Severity


def test_health_event_has_enrichment_fields_with_defaults():
    ev = HealthEvent(
        stream_id="cam",
        kind=HealthEventKind.ERROR,
        at_ns=1_000,
        detail="boom",
    )
    # new fields default to safe values when caller does not set them.
    assert ev.severity == Severity.INFO
    assert ev.source == "unknown"
    assert ev.fingerprint == ""
    assert ev.data == {}


def test_health_event_to_dict_includes_new_fields():
    ev = HealthEvent(
        stream_id="cam",
        kind=HealthEventKind.ERROR,
        at_ns=1_000,
        detail="boom",
        severity=Severity.ERROR,
        source="adapter:oak",
        fingerprint="cam:adapter:xlink-error",
        data={"stream": "__x_0_1"},
    )
    d = ev.to_dict()
    assert d["severity"] == "error"
    assert d["source"] == "adapter:oak"
    assert d["fingerprint"] == "cam:adapter:xlink-error"
    assert d["data"] == {"stream": "__x_0_1"}
