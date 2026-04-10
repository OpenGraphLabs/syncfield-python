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
