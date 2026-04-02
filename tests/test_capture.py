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


# --- record() tests ---


def test_record_basic_flow(tmp_path: Path):
    session = SyncSession(host_id="h1", output_dir=tmp_path)
    session.start()

    for i in range(3):
        session.record("imu", frame_number=i, channels={"x": float(i)})

    session.stop()

    ts_path = tmp_path / "imu.timestamps.jsonl"
    sensor_path = tmp_path / "imu.jsonl"
    assert ts_path.exists()
    assert sensor_path.exists()

    ts_lines = ts_path.read_text().strip().split("\n")
    sensor_lines = sensor_path.read_text().strip().split("\n")
    assert len(ts_lines) == 3
    assert len(sensor_lines) == 3


def test_record_sensor_jsonl_content(tmp_path: Path):
    session = SyncSession(host_id="rig_01", output_dir=tmp_path)
    session.start()

    session.record("imu", frame_number=0, channels={"accel_x": 0.5, "accel_y": -1.2})

    session.stop()

    line = json.loads((tmp_path / "imu.jsonl").read_text().strip())
    assert line["channels"] == {"accel_x": 0.5, "accel_y": -1.2}
    assert line["capture_ns"] > 0
    assert line["frame_number"] == 0
    assert line["clock_source"] == "host_monotonic"
    assert line["clock_domain"] == "rig_01"
    assert line["uncertainty_ns"] == 5_000_000


def test_record_timestamps_match_sensor(tmp_path: Path):
    session = SyncSession(host_id="h1", output_dir=tmp_path)
    session.start()

    for i in range(5):
        session.record("sensor", frame_number=i, channels={"v": float(i)})

    session.stop()

    ts_lines = (tmp_path / "sensor.timestamps.jsonl").read_text().strip().split("\n")
    sensor_lines = (tmp_path / "sensor.jsonl").read_text().strip().split("\n")

    for ts_raw, sensor_raw in zip(ts_lines, sensor_lines):
        ts = json.loads(ts_raw)
        sensor = json.loads(sensor_raw)
        assert ts["capture_ns"] == sensor["capture_ns"]
        assert ts["frame_number"] == sensor["frame_number"]


def test_record_returns_capture_ns(tmp_path: Path):
    session = SyncSession(host_id="h1", output_dir=tmp_path)
    session.start()

    result = session.record("imu", frame_number=0, channels={"x": 1.0})

    session.stop()

    assert isinstance(result, int)
    assert result > 0


def test_record_before_start_raises(tmp_path: Path):
    session = SyncSession(host_id="h", output_dir=tmp_path)
    try:
        session.record("imu", frame_number=0, channels={"x": 1.0})
        assert False, "should have raised"
    except RuntimeError:
        pass


def test_record_thread_safety(tmp_path: Path):
    """Record from multiple threads concurrently."""
    session = SyncSession(host_id="mt", output_dir=tmp_path)
    session.start()

    errors: list[Exception] = []

    def record_stream(stream_id: str, count: int) -> None:
        try:
            for i in range(count):
                session.record(stream_id, frame_number=i, channels={"v": float(i)})
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=record_stream, args=(f"s{i}", 200))
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


# --- link() tests ---


def test_link_basic(tmp_path: Path):
    session = SyncSession(host_id="h1", output_dir=tmp_path)
    session.start()

    session.link("cam_left", "/data/video.mp4")

    session.stop()

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert "cam_left" in manifest["streams"]
    assert manifest["streams"]["cam_left"]["path"] == "/data/video.mp4"


def test_link_with_stamp(tmp_path: Path):
    """Video pattern: stamp() for timestamps, link() for the file path."""
    session = SyncSession(host_id="h1", output_dir=tmp_path)
    session.start()

    for i in range(3):
        session.stamp("cam_left", frame_number=i)

    session.link("cam_left", "/data/video.mp4")

    session.stop()

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    entry = manifest["streams"]["cam_left"]
    assert entry["type"] == "video"
    assert entry["path"] == "/data/video.mp4"
    assert entry["timestamps_path"] == "cam_left.timestamps.jsonl"


# --- manifest tests ---


def test_manifest_written_on_stop(tmp_path: Path):
    session = SyncSession(host_id="h1", output_dir=tmp_path)
    session.start()
    session.stamp("cam", frame_number=0)
    session.stop()

    assert (tmp_path / "manifest.json").exists()


def test_manifest_sensor_stream(tmp_path: Path):
    session = SyncSession(host_id="h1", output_dir=tmp_path)
    session.start()

    session.record("imu", frame_number=0, channels={"x": 1.0})

    session.stop()

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    entry = manifest["streams"]["imu"]
    assert entry["type"] == "sensor"
    assert entry["sensor_path"] == "imu.jsonl"
    assert entry["timestamps_path"] == "imu.timestamps.jsonl"


def test_manifest_video_stream(tmp_path: Path):
    session = SyncSession(host_id="h1", output_dir=tmp_path)
    session.start()

    for i in range(3):
        session.stamp("cam", frame_number=i)
    session.link("cam", "/data/cam.mp4")

    session.stop()

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    entry = manifest["streams"]["cam"]
    assert entry["type"] == "video"
    assert entry["path"] == "/data/cam.mp4"
    assert entry["timestamps_path"] == "cam.timestamps.jsonl"


def test_manifest_mixed_streams(tmp_path: Path):
    """Session with both video (stamp+link) and sensor (record) streams."""
    session = SyncSession(host_id="rig_01", output_dir=tmp_path)
    session.start()

    # Video stream: stamp + link
    for i in range(5):
        session.stamp("cam_left", frame_number=i)
    session.link("cam_left", "/data/cam_left.mp4")

    # Sensor stream: record
    for i in range(10):
        session.record("imu", frame_number=i, channels={"ax": float(i), "ay": 0.0})

    counts = session.stop()

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["host_id"] == "rig_01"
    assert "sdk_version" in manifest

    # Video stream checks
    cam = manifest["streams"]["cam_left"]
    assert cam["type"] == "video"
    assert cam["path"] == "/data/cam_left.mp4"
    assert cam["timestamps_path"] == "cam_left.timestamps.jsonl"
    assert cam["frame_count"] == 5

    # Sensor stream checks
    imu = manifest["streams"]["imu"]
    assert imu["type"] == "sensor"
    assert imu["sensor_path"] == "imu.jsonl"
    assert imu["timestamps_path"] == "imu.timestamps.jsonl"
    assert imu["frame_count"] == 10

    # Counts from stop()
    assert counts["cam_left"] == 5
    assert counts["imu"] == 10
