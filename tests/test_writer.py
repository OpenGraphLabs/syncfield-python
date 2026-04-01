"""Tests for syncfield.writer."""

import json
from pathlib import Path

from syncfield.types import FrameTimestamp, SyncPoint
from syncfield.writer import StreamWriter, write_sync_point


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
    assert data["sdk_version"] == "0.1.0"
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
