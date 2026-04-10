"""Unit tests for PushSensorStream."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from syncfield.adapters.push_sensor import PushSensorStream
from syncfield.clock import SessionClock
from syncfield.types import (
    HealthEventKind, SampleEvent, StreamCapabilities, SyncPoint,
)


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


# ---------------------------------------------------------------------------
# Task 10: Skeleton tests
# ---------------------------------------------------------------------------

def test_push_sensor_minimal_construction(tmp_path: Path):
    stream = PushSensorStream("ble_imu", output_dir=tmp_path)
    assert stream.id == "ble_imu"
    assert stream.kind == "sensor"
    assert stream.capabilities.supports_precise_timestamps is False
    assert stream.capabilities.produces_file is True


def test_push_sensor_user_capabilities_override(tmp_path: Path):
    user = StreamCapabilities(
        provides_audio_track=False, supports_precise_timestamps=True,
        is_removable=True, produces_file=True,
    )
    stream = PushSensorStream("ble", output_dir=tmp_path, capabilities=user)
    assert stream.capabilities.is_removable is True
    assert stream.capabilities.supports_precise_timestamps is True


def test_push_sensor_device_key(tmp_path: Path):
    stream = PushSensorStream(
        "ble", output_dir=tmp_path, device_key=("ble", "AA:BB:CC:DD:EE:FF"),
    )
    assert stream.device_key == ("ble", "AA:BB:CC:DD:EE:FF")


def test_push_sensor_default_device_key_is_none(tmp_path: Path):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    assert stream.device_key is None


# ---------------------------------------------------------------------------
# Task 11: 4-phase lifecycle tests
# ---------------------------------------------------------------------------

def test_connect_invokes_on_connect_callback(tmp_path):
    captured = {"stream": None}
    def on_connect(stream):
        captured["stream"] = stream
    stream = PushSensorStream("ble", output_dir=tmp_path, on_connect=on_connect)
    stream.connect()
    assert captured["stream"] is stream
    assert stream._connected is True
    stream.disconnect()


def test_disconnect_invokes_on_disconnect_callback(tmp_path):
    captured = {"stream": None}
    def on_disconnect(stream):
        captured["stream"] = stream
    stream = PushSensorStream("ble", output_dir=tmp_path, on_disconnect=on_disconnect)
    stream.connect()
    stream.disconnect()
    assert captured["stream"] is stream
    assert stream._connected is False


def test_lifecycle_without_callbacks(tmp_path):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    stream.start_recording(_clock())
    report = stream.stop_recording()
    stream.disconnect()
    assert report.status == "completed"
    assert report.frame_count == 0


def test_start_recording_opens_writer(tmp_path):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    stream.start_recording(_clock())
    assert stream._writing is True
    assert (tmp_path / "ble.jsonl").exists()
    stream.stop_recording()
    stream.disconnect()


def test_stop_recording_returns_finalization_report(tmp_path):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    stream.start_recording(_clock())
    report = stream.stop_recording()
    stream.disconnect()
    assert report.stream_id == "ble"
    assert report.file_path == tmp_path / "ble.jsonl"
    assert report.error is None


# ---------------------------------------------------------------------------
# Task 12: push() happy path tests
# ---------------------------------------------------------------------------

def test_push_emits_sample_when_connected(tmp_path):
    samples: list[SampleEvent] = []
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.on_sample(samples.append)
    stream.connect()
    stream.push({"ax": 0.5})
    assert len(samples) == 1
    assert samples[0].channels == {"ax": 0.5}
    assert samples[0].frame_number == 0
    assert samples[0].capture_ns > 0
    stream.disconnect()


def test_push_default_capture_ns_uses_monotonic_now(tmp_path):
    samples: list[SampleEvent] = []
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.on_sample(samples.append)
    stream.connect()
    before = time.monotonic_ns()
    stream.push({"x": 1})
    after = time.monotonic_ns()
    assert before <= samples[0].capture_ns <= after
    stream.disconnect()


def test_push_explicit_capture_ns_preserved(tmp_path):
    samples: list[SampleEvent] = []
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.on_sample(samples.append)
    stream.connect()
    stream.push({"x": 1}, capture_ns=1234567890)
    assert samples[0].capture_ns == 1234567890
    stream.disconnect()


def test_push_explicit_frame_number_preserved(tmp_path):
    samples: list[SampleEvent] = []
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.on_sample(samples.append)
    stream.connect()
    stream.push({"x": 1}, frame_number=42)
    assert samples[0].frame_number == 42
    stream.disconnect()


def test_push_does_not_write_outside_recording(tmp_path):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    stream.push({"x": 1})
    stream.push({"x": 2})
    stream.disconnect()
    assert not (tmp_path / "ble.jsonl").exists()


def test_push_writes_when_recording(tmp_path):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    stream.start_recording(_clock())
    stream.push({"x": 1})
    stream.push({"x": 2})
    stream.push({"x": 3})
    stream.stop_recording()
    stream.disconnect()
    lines = (tmp_path / "ble.jsonl").read_text().strip().split("\n")
    assert len(lines) == 3
    assert [json.loads(l)["channels"]["x"] for l in lines] == [1, 2, 3]


def test_push_frame_counter_continuous_across_recording_toggle(tmp_path):
    samples: list[SampleEvent] = []
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.on_sample(samples.append)
    stream.connect()
    stream.push({"x": 1})  # frame 0 (preview)
    stream.push({"x": 2})  # frame 1 (preview)
    stream.start_recording(_clock())
    stream.push({"x": 3})  # frame 2 (written)
    stream.push({"x": 4})  # frame 3 (written)
    stream.stop_recording()
    stream.disconnect()
    lines = (tmp_path / "ble.jsonl").read_text().strip().split("\n")
    written = [json.loads(l) for l in lines]
    assert [w["frame_number"] for w in written] == [2, 3]
    assert [s.frame_number for s in samples] == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# Task 13: push() error handling tests
# ---------------------------------------------------------------------------

def test_push_before_connect_drops_with_warning(tmp_path):
    health: list = []
    samples: list = []
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.on_sample(samples.append)
    stream.on_health(health.append)
    stream.push({"x": 1})
    assert samples == []
    warnings = [h for h in health if h.kind == HealthEventKind.WARNING]
    assert len(warnings) == 1
    assert "outside connect/disconnect" in (warnings[0].detail or "")


def test_push_after_disconnect_drops_with_warning(tmp_path):
    health: list = []
    samples: list = []
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.on_sample(samples.append)
    stream.on_health(health.append)
    stream.connect()
    stream.disconnect()
    stream.push({"x": 1})
    assert samples == []
    warnings = [h for h in health if h.kind == HealthEventKind.WARNING]
    assert len(warnings) == 1


def test_push_with_non_dict_channels_raises_typeerror(tmp_path):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    with pytest.raises(TypeError, match="dict"):
        stream.push([1, 2, 3])  # type: ignore[arg-type]
    stream.disconnect()


def test_push_never_raises_for_internal_failures(tmp_path, monkeypatch):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    stream.start_recording(_clock())
    def boom(_sample):
        raise OSError("disk full")
    monkeypatch.setattr(stream._write_core, "write", boom)
    health: list = []
    stream.on_health(health.append)
    stream.push({"x": 1})  # must not raise
    errors = [h for h in health if h.kind == HealthEventKind.ERROR]
    assert any("disk full" in (e.detail or "") for e in errors)
    stream.stop_recording()
    stream.disconnect()


# ---------------------------------------------------------------------------
# Task 14: re-export test
# ---------------------------------------------------------------------------

def test_push_sensor_stream_is_re_exported_from_adapters_package():
    from syncfield.adapters import PushSensorStream as Reexported
    assert Reexported is PushSensorStream
