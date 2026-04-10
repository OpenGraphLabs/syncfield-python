"""Unit tests for PollingSensorStream — drive without spawning a thread."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from syncfield.adapters.polling_sensor import PollingSensorStream
from syncfield.clock import SessionClock
from syncfield.types import (
    HealthEventKind,
    SampleEvent,
    StreamCapabilities,
    SyncPoint,
)


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


def test_polling_sensor_minimal_construction(tmp_path: Path):
    def read():
        return {"x": 1.0}
    stream = PollingSensorStream("imu", read=read, hz=100, output_dir=tmp_path)
    assert stream.id == "imu"
    assert stream.kind == "sensor"
    assert stream.capabilities.supports_precise_timestamps is True
    assert stream.capabilities.produces_file is True


def test_polling_sensor_with_open_close(tmp_path: Path):
    def open_dev():
        return {"handle": True}
    def read(handle):
        return {"x": handle["handle"]}
    def close(handle):
        handle["handle"] = False
    stream = PollingSensorStream(
        "env", read=read, open=open_dev, close=close, hz=10, output_dir=tmp_path,
    )
    assert stream.id == "env"


def test_polling_sensor_arity_mismatch_with_open_raises(tmp_path: Path):
    with pytest.raises(TypeError, match="read"):
        PollingSensorStream(
            "bad", read=lambda: {"x": 1}, open=lambda: None,
            hz=10, output_dir=tmp_path,
        )


def test_polling_sensor_arity_mismatch_without_open_raises(tmp_path: Path):
    with pytest.raises(TypeError, match="read"):
        PollingSensorStream("bad", read=lambda handle: {"x": 1}, hz=10, output_dir=tmp_path)


def test_polling_sensor_user_capabilities_override(tmp_path: Path):
    user_caps = StreamCapabilities(
        provides_audio_track=False, supports_precise_timestamps=True,
        is_removable=True, produces_file=True,
    )
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=100,
        output_dir=tmp_path, capabilities=user_caps,
    )
    assert stream.capabilities.is_removable is True


def test_polling_sensor_device_key(tmp_path: Path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=100,
        output_dir=tmp_path, device_key=("serial", "/dev/ttyUSB0"),
    )
    assert stream.device_key == ("serial", "/dev/ttyUSB0")


def test_polling_sensor_default_device_key_is_none(tmp_path: Path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=100, output_dir=tmp_path,
    )
    assert stream.device_key is None


# ---------------------------------------------------------------------------
# Task 5: _capture_once() happy path (emit only, no disk)
# ---------------------------------------------------------------------------

def test_capture_once_emits_sample_with_channels(tmp_path):
    samples: list[SampleEvent] = []
    def read():
        return {"ax": 0.5, "ay": -0.3}
    stream = PollingSensorStream("imu", read=read, hz=100, output_dir=tmp_path)
    stream.on_sample(samples.append)
    cont = stream._capture_once()
    assert cont is True
    assert len(samples) == 1
    assert samples[0].stream_id == "imu"
    assert samples[0].frame_number == 0
    assert samples[0].channels == {"ax": 0.5, "ay": -0.3}
    assert samples[0].capture_ns > 0


def test_capture_once_increments_frame_number(tmp_path):
    samples: list[SampleEvent] = []
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=100, output_dir=tmp_path,
    )
    stream.on_sample(samples.append)
    for _ in range(5):
        stream._capture_once()
    assert [s.frame_number for s in samples] == [0, 1, 2, 3, 4]


def test_capture_once_passes_handle_when_open_provided(tmp_path):
    samples: list[SampleEvent] = []
    def read(handle):
        return {"value": handle["v"]}
    stream = PollingSensorStream(
        "x", read=read, open=lambda: {"v": 42},
        hz=100, output_dir=tmp_path,
    )
    stream._handle = {"v": 42}  # bypass connect() for unit test
    stream.on_sample(samples.append)
    stream._capture_once()
    assert samples[0].channels == {"value": 42}


# ---------------------------------------------------------------------------
# Task 6: _capture_once() writes to disk when _writing=True
# ---------------------------------------------------------------------------

def test_capture_once_does_not_write_when_not_recording(tmp_path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=100, output_dir=tmp_path,
    )
    for _ in range(3):
        stream._capture_once()
    assert not (tmp_path / "imu.jsonl").exists()


def test_capture_once_writes_when_recording(tmp_path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1, "y": 2}, hz=100, output_dir=tmp_path,
    )
    stream._write_core.open()
    stream._writing = True
    for _ in range(3):
        stream._capture_once()
    stream._write_core.close()
    lines = (tmp_path / "imu.jsonl").read_text().strip().split("\n")
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["frame_number"] == 0
    assert first["channels"] == {"x": 1, "y": 2}


def test_capture_once_frame_counter_continuous_across_recording_toggle(tmp_path):
    """Frame counter is monotonic — preview samples advance it too."""
    samples: list = []
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=100, output_dir=tmp_path,
    )
    stream.on_sample(samples.append)
    stream._capture_once()  # frame 0
    stream._capture_once()  # frame 1
    stream._write_core.open()
    stream._writing = True
    stream._capture_once()  # frame 2 (first written)
    stream._capture_once()  # frame 3
    stream._write_core.close()
    lines = (tmp_path / "imu.jsonl").read_text().strip().split("\n")
    written = [json.loads(l) for l in lines]
    assert [w["frame_number"] for w in written] == [2, 3]
    assert [s.frame_number for s in samples] == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# Task 7: _capture_once() error handling
# ---------------------------------------------------------------------------

def test_capture_once_drop_on_read_error_default(tmp_path):
    health: list = []
    calls = {"n": 0}
    def read():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("device disconnected")
        return {"x": 1}
    stream = PollingSensorStream("imu", read=read, hz=100, output_dir=tmp_path)
    stream.on_health(health.append)
    cont1 = stream._capture_once()
    cont2 = stream._capture_once()
    assert cont1 is True
    assert cont2 is True
    assert len(health) == 1
    assert health[0].kind == HealthEventKind.ERROR
    assert "device disconnected" in (health[0].detail or "")


def test_capture_once_stop_on_read_error_when_configured(tmp_path):
    def read():
        raise RuntimeError("permanent failure")
    stream = PollingSensorStream(
        "imu", read=read, hz=100, output_dir=tmp_path, on_read_error="stop",
    )
    cont = stream._capture_once()
    assert cont is False


def test_capture_once_drop_on_non_dict_return(tmp_path):
    health: list = []
    calls = {"n": 0}
    def read():
        calls["n"] += 1
        if calls["n"] == 1:
            return [1, 2, 3]
        return {"x": 1}
    stream = PollingSensorStream("imu", read=read, hz=100, output_dir=tmp_path)
    stream.on_health(health.append)
    cont1 = stream._capture_once()
    cont2 = stream._capture_once()
    assert cont1 is True
    assert cont2 is True
    assert len(health) == 1
    assert health[0].kind == HealthEventKind.ERROR
    assert "list" in (health[0].detail or "")


def test_capture_once_stop_on_non_dict_return_when_configured(tmp_path):
    stream = PollingSensorStream(
        "imu", read=lambda: "nope", hz=100,
        output_dir=tmp_path, on_read_error="stop",
    )
    assert stream._capture_once() is False


# ---------------------------------------------------------------------------
# Task 8: 4-phase lifecycle (connect, disconnect, start/stop_recording)
# ---------------------------------------------------------------------------

def test_connect_calls_open_and_spawns_thread(tmp_path):
    opened = {"called": False}
    def open_dev():
        opened["called"] = True
        return {"port": "/dev/ttyX"}
    def read(handle):
        return {"x": 1}
    def close(handle):
        pass
    stream = PollingSensorStream(
        "imu", read=read, open=open_dev, close=close,
        hz=1000, output_dir=tmp_path,
    )
    stream.connect()
    try:
        assert opened["called"] is True
        assert stream._handle == {"port": "/dev/ttyX"}
        assert stream._thread is not None
        assert stream._thread.is_alive()
    finally:
        stream.disconnect()


def test_disconnect_joins_thread_and_calls_close(tmp_path):
    closed = {"called": False}
    def close(handle):
        closed["called"] = True
    stream = PollingSensorStream(
        "imu", read=lambda h: {"x": 1},
        open=lambda: "h", close=close,
        hz=1000, output_dir=tmp_path,
    )
    stream.connect()
    stream.disconnect()
    assert closed["called"] is True
    assert stream._handle is None
    assert not stream._thread.is_alive()


def test_disconnect_without_close_callback(tmp_path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=1000, output_dir=tmp_path,
    )
    stream.connect()
    stream.disconnect()


def test_open_raises_bubbles_out_of_connect(tmp_path):
    def open_dev():
        raise RuntimeError("permission denied")
    stream = PollingSensorStream(
        "imu", read=lambda h: {"x": 1}, open=open_dev,
        hz=100, output_dir=tmp_path,
    )
    with pytest.raises(RuntimeError, match="permission denied"):
        stream.connect()


def test_close_raises_emits_warning_but_disconnect_succeeds(tmp_path):
    health: list = []
    def close(handle):
        raise RuntimeError("close failed")
    stream = PollingSensorStream(
        "imu", read=lambda h: {"x": 1},
        open=lambda: "h", close=close,
        hz=1000, output_dir=tmp_path,
    )
    stream.on_health(health.append)
    stream.connect()
    stream.disconnect()
    warnings = [h for h in health if h.kind == HealthEventKind.WARNING]
    assert any("close failed" in (w.detail or "") for w in warnings)


def test_start_recording_opens_writer_and_flips_writing(tmp_path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=1000, output_dir=tmp_path,
    )
    stream.connect()
    try:
        stream.start_recording(_clock())
        assert stream._writing is True
        assert (tmp_path / "imu.jsonl").exists()
    finally:
        stream.stop_recording()
        stream.disconnect()


def test_stop_recording_returns_finalization_report(tmp_path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=1000, output_dir=tmp_path,
    )
    stream.connect()
    stream.start_recording(_clock())
    time.sleep(0.05)
    report = stream.stop_recording()
    stream.disconnect()
    assert report.stream_id == "imu"
    assert report.status == "completed"
    assert report.frame_count > 0
    assert report.file_path == tmp_path / "imu.jsonl"
    assert report.first_sample_at_ns is not None
    assert report.last_sample_at_ns is not None
    assert report.last_sample_at_ns >= report.first_sample_at_ns
    assert report.error is None


# ---------------------------------------------------------------------------
# Task 9: re-export from adapters package
# ---------------------------------------------------------------------------

def test_polling_sensor_stream_is_re_exported_from_adapters_package():
    from syncfield.adapters import PollingSensorStream as Reexported
    assert Reexported is PollingSensorStream
