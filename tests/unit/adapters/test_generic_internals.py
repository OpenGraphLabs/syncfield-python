"""Tests for syncfield.adapters._generic internals."""

from __future__ import annotations

from pathlib import Path

from syncfield.adapters._generic import _SensorWriteCore


def test_sensor_write_core_frame_counter_starts_at_zero(tmp_path: Path):
    core = _SensorWriteCore("imu", tmp_path)
    assert core.next_frame_number() == 0
    assert core.next_frame_number() == 1
    assert core.next_frame_number() == 2


def test_sensor_write_core_open_creates_jsonl_file(tmp_path: Path):
    core = _SensorWriteCore("imu", tmp_path)
    core.open()
    assert (tmp_path / "imu.jsonl").exists()
    core.close()


def test_sensor_write_core_close_is_idempotent(tmp_path: Path):
    core = _SensorWriteCore("imu", tmp_path)
    core.open()
    core.close()
    core.close()  # must not raise


def test_sensor_write_core_path_property(tmp_path: Path):
    core = _SensorWriteCore("imu", tmp_path)
    assert core.path == tmp_path / "imu.jsonl"


import json
import threading

from syncfield.types import SensorSample


def test_sensor_write_core_write_appends_jsonl(tmp_path):
    core = _SensorWriteCore("imu", tmp_path)
    core.open()
    core.write(SensorSample(frame_number=0, capture_ns=1000,
                            channels={"ax": 0.1}))
    core.write(SensorSample(frame_number=1, capture_ns=2000,
                            channels={"ax": 0.2}))
    core.close()

    lines = (tmp_path / "imu.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["frame_number"] == 0
    assert first["capture_ns"] == 1000
    assert first["channels"] == {"ax": 0.1}


def test_sensor_write_core_tracks_first_and_last_at(tmp_path):
    core = _SensorWriteCore("imu", tmp_path)
    core.open()
    core.write(SensorSample(frame_number=0, capture_ns=1000, channels={"x": 1}))
    core.write(SensorSample(frame_number=1, capture_ns=2500, channels={"x": 2}))
    core.write(SensorSample(frame_number=2, capture_ns=3700, channels={"x": 3}))
    assert core.first_sample_at_ns == 1000
    assert core.last_sample_at_ns == 3700
    assert core.frame_count == 3
    core.close()


def test_sensor_write_core_write_is_thread_safe(tmp_path):
    """100 producer threads x 50 writes each = 5000 lines, all intact."""
    core = _SensorWriteCore("imu", tmp_path)
    core.open()

    def producer(tid: int) -> None:
        for i in range(50):
            core.write(SensorSample(
                frame_number=core.next_frame_number(),
                capture_ns=1000 + tid * 1000 + i,
                channels={"tid": tid, "i": i},
            ))

    threads = [threading.Thread(target=producer, args=(t,)) for t in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    core.close()

    lines = (tmp_path / "imu.jsonl").read_text().strip().split("\n")
    assert len(lines) == 5000
    for line in lines:
        json.loads(line)
