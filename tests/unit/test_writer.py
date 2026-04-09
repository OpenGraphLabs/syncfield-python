"""Tests for syncfield.writer."""

import json
from pathlib import Path

from syncfield.types import (
    ChirpSpec,
    FrameTimestamp,
    HealthEvent,
    HealthEventKind,
    SensorSample,
    StreamCapabilities,
    SyncPoint,
)
from syncfield.writer import (
    SensorWriter,
    SessionLogWriter,
    StreamWriter,
    write_manifest,
    write_sync_point,
)


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


# --- sync_point.json chirp extensions ---


class TestSyncPointWithChirp:
    def test_writes_chirp_fields_when_provided(self, tmp_path: Path):
        sp = SyncPoint.create_now("h")
        spec = ChirpSpec(400, 2500, 500, 0.8, 15)
        path = write_sync_point(
            sp,
            tmp_path,
            chirp_start_ns=1_000_000_000,
            chirp_stop_ns=5_000_000_000,
            chirp_spec=spec,
        )
        data = json.loads(path.read_text())
        assert data["chirp_start_ns"] == 1_000_000_000
        assert data["chirp_stop_ns"] == 5_000_000_000
        assert data["chirp_spec"] == spec.to_dict()

    def test_omits_chirp_fields_when_none(self, tmp_path: Path):
        sp = SyncPoint.create_now("h")
        path = write_sync_point(sp, tmp_path)
        data = json.loads(path.read_text())
        assert "chirp_start_ns" not in data
        assert "chirp_stop_ns" not in data
        assert "chirp_spec" not in data


# --- manifest.json capability round-trip ---


class TestManifestWithCapabilities:
    def test_writes_capabilities_when_provided(self, tmp_path: Path):
        caps = StreamCapabilities(provides_audio_track=True, produces_file=True)
        streams = {
            "cam": {
                "type": "video",
                "path": "cam.mp4",
                "capabilities": caps.to_dict(),
            }
        }
        path = write_manifest("h", streams, tmp_path)
        data = json.loads(path.read_text())
        assert data["streams"]["cam"]["capabilities"]["provides_audio_track"] is True
        assert data["streams"]["cam"]["capabilities"]["produces_file"] is True


# --- SessionLogWriter ---


class TestSessionLogWriter:
    def test_writes_events_and_health_as_jsonl(self, tmp_path: Path):
        writer = SessionLogWriter(tmp_path)
        writer.open()
        writer.log_event(
            {
                "kind": "state_transition",
                "from": "idle",
                "to": "preparing",
                "at_ns": 100,
            }
        )
        writer.log_health(
            HealthEvent("cam", HealthEventKind.HEARTBEAT, at_ns=200, detail=None)
        )
        writer.close()

        lines = (tmp_path / "session_log.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2
        ev1 = json.loads(lines[0])
        ev2 = json.loads(lines[1])
        assert ev1["kind"] == "state_transition"
        assert ev1["from"] == "idle"
        assert ev2["kind"] == "health"
        assert ev2["stream_id"] == "cam"
        assert ev2["health_kind"] == "heartbeat"

    def test_flushes_on_every_write(self, tmp_path: Path):
        """Log lines must survive a process crash mid-recording."""
        writer = SessionLogWriter(tmp_path)
        writer.open()
        writer.log_event({"kind": "test", "at_ns": 1})
        # Without calling close(), the line must already be on disk
        content = (tmp_path / "session_log.jsonl").read_text()
        assert "test" in content
        writer.close()

    def test_log_event_before_open_raises(self, tmp_path: Path):
        writer = SessionLogWriter(tmp_path)
        try:
            writer.log_event({"kind": "x", "at_ns": 1})
            assert False, "should have raised"
        except RuntimeError:
            pass

    def test_path_property_points_at_session_log_jsonl(self, tmp_path: Path):
        writer = SessionLogWriter(tmp_path)
        assert writer.path == tmp_path / "session_log.jsonl"
