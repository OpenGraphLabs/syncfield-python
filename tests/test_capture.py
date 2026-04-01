"""Tests for syncfield.capture (SyncSession)."""

import json
import threading
from pathlib import Path

from syncfield.capture import SyncSession


def test_basic_session_flow(tmp_path: Path):
    session = SyncSession(host_id="rig_01", output_dir=tmp_path / "out")
    sp = session.start()

    assert sp.host_id == "rig_01"
    assert sp.monotonic_ns > 0

    for i in range(5):
        session.stamp("cam_left", frame_number=i)
        session.stamp("cam_right", frame_number=i)

    counts = session.stop()
    assert counts == {"cam_left": 5, "cam_right": 5}

    # Check output files
    out = tmp_path / "out"
    assert (out / "sync_point.json").exists()
    assert (out / "cam_left.timestamps.jsonl").exists()
    assert (out / "cam_right.timestamps.jsonl").exists()

    # Verify JSONL content
    lines = (out / "cam_left.timestamps.jsonl").read_text().strip().split("\n")
    assert len(lines) == 5
    first = json.loads(lines[0])
    assert first["frame_number"] == 0
    assert first["clock_source"] == "host_monotonic"
    assert first["clock_domain"] == "rig_01"


def test_timestamps_are_monotonically_increasing(tmp_path: Path):
    session = SyncSession(host_id="h1", output_dir=tmp_path)
    session.start()

    for i in range(100):
        session.stamp("stream", frame_number=i)

    session.stop()

    lines = (tmp_path / "stream.timestamps.jsonl").read_text().strip().split("\n")
    timestamps = [json.loads(line)["capture_ns"] for line in lines]
    for a, b in zip(timestamps, timestamps[1:]):
        assert b >= a, f"Non-monotonic: {a} -> {b}"


def test_thread_safety(tmp_path: Path):
    """Stamp from multiple threads concurrently."""
    session = SyncSession(host_id="mt", output_dir=tmp_path)
    session.start()

    errors: list[Exception] = []

    def stamp_stream(stream_id: str, count: int) -> None:
        try:
            for i in range(count):
                session.stamp(stream_id, frame_number=i)
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=stamp_stream, args=(f"s{i}", 200))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    counts = session.stop()
    assert not errors
    assert len(counts) == 4
    for sid, c in counts.items():
        assert c == 200


def test_stamp_before_start_raises(tmp_path: Path):
    session = SyncSession(host_id="h", output_dir=tmp_path)
    try:
        session.stamp("x", frame_number=0)
        assert False, "should have raised"
    except RuntimeError:
        pass


def test_double_start_raises(tmp_path: Path):
    session = SyncSession(host_id="h", output_dir=tmp_path)
    session.start()
    try:
        session.start()
        assert False, "should have raised"
    except RuntimeError:
        pass
    session.stop()


def test_custom_uncertainty(tmp_path: Path):
    session = SyncSession(host_id="h", output_dir=tmp_path)
    session.start()
    session.stamp("imu", frame_number=0, uncertainty_ns=1_000_000)
    session.stop()

    line = json.loads((tmp_path / "imu.timestamps.jsonl").read_text().strip())
    assert line["uncertainty_ns"] == 1_000_000


def test_sync_point_json_content(tmp_path: Path):
    session = SyncSession(host_id="rig_02", output_dir=tmp_path)
    sp = session.start()
    session.stop()

    data = json.loads((tmp_path / "sync_point.json").read_text())
    assert data["host_id"] == "rig_02"
    assert data["monotonic_ns"] == sp.monotonic_ns
    assert data["sdk_version"] == "0.1.0"
