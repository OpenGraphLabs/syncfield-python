"""Tests for syncfield.writer."""

import json
from pathlib import Path

from syncfield.types import FrameTimestamp, SensorSample, SyncPoint
from syncfield.writer import SensorWriter, StreamWriter, write_manifest, write_sync_point


def test_stream_writer_creates_jsonl(tmp_path: Path):
    w = StreamWriter("cam_left", tmp_path)
    w.open()
    for i in range(3):
        w.write(FrameTimestamp(frame_number=i, capture_ns=1000 + i, clock_domain="h1"))
    w.close()

    path = tmp_path / "cam_left.timestamps.jsonl"
    assert path.exists()

    lines = path.read_text().strip().split("\n")
    assert len(lines) == 3

    first = json.loads(lines[0])
    assert first["frame_number"] == 0
    assert first["capture_ns"] == 1000
    assert first["clock_source"] == "host_monotonic"
    assert first["clock_domain"] == "h1"


def test_stream_writer_count(tmp_path: Path):
    w = StreamWriter("imu", tmp_path)
    w.open()
    assert w.count == 0
    w.write(FrameTimestamp(frame_number=0, capture_ns=100))
    w.write(FrameTimestamp(frame_number=1, capture_ns=200))
    assert w.count == 2
    w.close()


def test_write_sync_point(tmp_path: Path):
    sp = SyncPoint(
        monotonic_ns=111,
        wall_clock_ns=222,
        host_id="test",
        timestamp_ms=333,
        iso_datetime="2024-01-01T00:00:00",
    )
    path = write_sync_point(sp, tmp_path)
    assert path == tmp_path / "sync_point.json"

    data = json.loads(path.read_text())
    assert data["sdk_version"] == "0.2.0"
    assert data["host_id"] == "test"
    assert data["monotonic_ns"] == 111
    assert data["wall_clock_ns"] == 222


def test_stream_writer_raises_if_not_open(tmp_path: Path):
    w = StreamWriter("x", tmp_path)
    try:
        w.write(FrameTimestamp(frame_number=0, capture_ns=1))
        assert False, "should have raised"
    except RuntimeError:
        pass


# --- SensorWriter tests ---


def test_sensor_writer_creates_jsonl(tmp_path: Path):
    w = SensorWriter("imu", tmp_path)
    w.open()
    for i in range(3):
        w.write(SensorSample(
            frame_number=i,
            capture_ns=1000 + i,
            channels={"accel_x": float(i), "accel_y": float(i * 2)},
            clock_domain="h1",
        ))
    w.close()

    path = tmp_path / "imu.jsonl"
    assert path.exists()

    lines = path.read_text().strip().split("\n")
    assert len(lines) == 3

    first = json.loads(lines[0])
    assert first["frame_number"] == 0
    assert first["capture_ns"] == 1000
    assert first["channels"] == {"accel_x": 0.0, "accel_y": 0.0}
    assert first["clock_domain"] == "h1"


def test_sensor_writer_count(tmp_path: Path):
    w = SensorWriter("sensor", tmp_path)
    w.open()
    assert w.count == 0
    w.write(SensorSample(frame_number=0, capture_ns=100, channels={"v": 1.0}))
    w.write(SensorSample(frame_number=1, capture_ns=200, channels={"v": 2.0}))
    assert w.count == 2
    w.close()


def test_sensor_writer_raises_if_not_open(tmp_path: Path):
    w = SensorWriter("x", tmp_path)
    try:
        w.write(SensorSample(frame_number=0, capture_ns=1, channels={"v": 0.0}))
        assert False, "should have raised"
    except RuntimeError:
        pass


def test_write_manifest(tmp_path: Path):
    streams = {
        "cam_left": {"type": "video", "timestamps_path": "cam_left.timestamps.jsonl"},
    }
    path = write_manifest("test_host", streams, tmp_path)
    assert path == tmp_path / "manifest.json"

    data = json.loads(path.read_text())
    assert data["sdk_version"] == "0.2.0"
    assert data["host_id"] == "test_host"
    assert "streams" in data
    assert data["streams"]["cam_left"]["type"] == "video"
