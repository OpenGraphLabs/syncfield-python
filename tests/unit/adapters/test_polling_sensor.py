"""Unit tests for PollingSensorStream — drive without spawning a thread."""

from __future__ import annotations

import time

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


def test_polling_sensor_minimal_construction():
    def read():
        return {"x": 1.0}
    stream = PollingSensorStream("imu", read=read, hz=100)
    assert stream.id == "imu"
    assert stream.kind == "sensor"
    assert stream.capabilities.supports_precise_timestamps is True
    assert stream.capabilities.produces_file is False


def test_polling_sensor_with_open_close():
    def open_dev():
        return {"handle": True}
    def read(handle):
        return {"x": handle["handle"]}
    def close(handle):
        handle["handle"] = False
    stream = PollingSensorStream(
        "env", read=read, open=open_dev, close=close, hz=10,
    )
    assert stream.id == "env"


def test_polling_sensor_arity_mismatch_with_open_raises():
    with pytest.raises(TypeError, match="read"):
        PollingSensorStream(
            "bad", read=lambda: {"x": 1}, open=lambda: None, hz=10,
        )


def test_polling_sensor_arity_mismatch_without_open_raises():
    with pytest.raises(TypeError, match="read"):
        PollingSensorStream("bad", read=lambda handle: {"x": 1}, hz=10)


def test_polling_sensor_user_capabilities_override():
    user_caps = StreamCapabilities(
        provides_audio_track=False, supports_precise_timestamps=True,
        is_removable=True, produces_file=True,
    )
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=100, capabilities=user_caps,
    )
    assert stream.capabilities.is_removable is True


def test_polling_sensor_device_key():
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=100,
        device_key=("serial", "/dev/ttyUSB0"),
    )
    assert stream.device_key == ("serial", "/dev/ttyUSB0")


def test_polling_sensor_default_device_key_is_none():
    stream = PollingSensorStream("imu", read=lambda: {"x": 1}, hz=100)
    assert stream.device_key is None


# ---------------------------------------------------------------------------
# Task 5: _capture_once() happy path (emit only, no disk)
# ---------------------------------------------------------------------------

def test_capture_once_emits_sample_with_channels():
    samples: list[SampleEvent] = []
    def read():
        return {"ax": 0.5, "ay": -0.3}
    stream = PollingSensorStream("imu", read=read, hz=100)
    stream.on_sample(samples.append)
    cont = stream._capture_once()
    assert cont is True
    assert len(samples) == 1
    assert samples[0].stream_id == "imu"
    assert samples[0].frame_number == 0
    assert samples[0].channels == {"ax": 0.5, "ay": -0.3}
    assert samples[0].capture_ns > 0


def test_capture_once_increments_frame_number():
    samples: list[SampleEvent] = []
    stream = PollingSensorStream("imu", read=lambda: {"x": 1}, hz=100)
    stream.on_sample(samples.append)
    for _ in range(5):
        stream._capture_once()
    assert [s.frame_number for s in samples] == [0, 1, 2, 3, 4]


def test_capture_once_passes_handle_when_open_provided():
    samples: list[SampleEvent] = []
    def read(handle):
        return {"value": handle["v"]}
    stream = PollingSensorStream(
        "x", read=read, open=lambda: {"v": 42}, hz=100,
    )
    stream._handle = {"v": 42}  # bypass connect() for unit test
    stream.on_sample(samples.append)
    stream._capture_once()
    assert samples[0].channels == {"value": 42}


# ---------------------------------------------------------------------------
# Task 6: _capture_once() recording tracking (no disk writes)
# ---------------------------------------------------------------------------

def test_capture_once_does_not_count_samples_when_not_recording():
    stream = PollingSensorStream("imu", read=lambda: {"x": 1}, hz=100)
    for _ in range(3):
        stream._capture_once()
    assert stream._write_core.recorded_count == 0


def test_capture_once_counts_samples_when_recording():
    stream = PollingSensorStream("imu", read=lambda: {"x": 1, "y": 2}, hz=100)
    stream._write_core.reset_recording_stats()
    stream._writing = True
    for _ in range(3):
        stream._capture_once()
    assert stream._write_core.recorded_count == 3


def test_capture_once_frame_counter_continuous_across_recording_toggle():
    """Frame counter is monotonic — preview samples advance it too."""
    samples: list[SampleEvent] = []
    stream = PollingSensorStream("imu", read=lambda: {"x": 1}, hz=100)
    stream.on_sample(samples.append)
    stream._capture_once()  # frame 0
    stream._capture_once()  # frame 1
    stream._write_core.reset_recording_stats()
    stream._writing = True
    stream._capture_once()  # frame 2 (first recorded)
    stream._capture_once()  # frame 3
    assert stream._write_core.recorded_count == 2
    assert [s.frame_number for s in samples] == [0, 1, 2, 3]
    # first recorded sample has frame_number 2
    assert stream._write_core.first_sample_at_ns is not None
    assert stream._write_core.last_sample_at_ns is not None


# ---------------------------------------------------------------------------
# Task 7: _capture_once() error handling
# ---------------------------------------------------------------------------

def test_capture_once_drop_on_read_error_default():
    health: list = []
    calls = {"n": 0}
    def read():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("device disconnected")
        return {"x": 1}
    stream = PollingSensorStream("imu", read=read, hz=100)
    stream.on_health(health.append)
    cont1 = stream._capture_once()
    cont2 = stream._capture_once()
    assert cont1 is True
    assert cont2 is True
    assert len(health) == 1
    assert health[0].kind == HealthEventKind.ERROR
    assert "device disconnected" in (health[0].detail or "")


def test_capture_once_stop_on_read_error_when_configured():
    def read():
        raise RuntimeError("permanent failure")
    stream = PollingSensorStream(
        "imu", read=read, hz=100, on_read_error="stop",
    )
    cont = stream._capture_once()
    assert cont is False


def test_capture_once_drop_on_non_dict_return():
    health: list = []
    calls = {"n": 0}
    def read():
        calls["n"] += 1
        if calls["n"] == 1:
            return [1, 2, 3]
        return {"x": 1}
    stream = PollingSensorStream("imu", read=read, hz=100)
    stream.on_health(health.append)
    cont1 = stream._capture_once()
    cont2 = stream._capture_once()
    assert cont1 is True
    assert cont2 is True
    assert len(health) == 1
    assert health[0].kind == HealthEventKind.ERROR
    assert "list" in (health[0].detail or "")


def test_capture_once_stop_on_non_dict_return_when_configured():
    stream = PollingSensorStream(
        "imu", read=lambda: "nope", hz=100, on_read_error="stop",
    )
    assert stream._capture_once() is False


# ---------------------------------------------------------------------------
# Task 8: 4-phase lifecycle (connect, disconnect, start/stop_recording)
# ---------------------------------------------------------------------------

def test_connect_calls_open_and_spawns_thread():
    opened = {"called": False}
    def open_dev():
        opened["called"] = True
        return {"port": "/dev/ttyX"}
    def read(handle):
        return {"x": 1}
    def close(handle):
        pass
    stream = PollingSensorStream(
        "imu", read=read, open=open_dev, close=close, hz=1000,
    )
    stream.connect()
    try:
        assert opened["called"] is True
        assert stream._handle == {"port": "/dev/ttyX"}
        assert stream._thread is not None
        assert stream._thread.is_alive()
    finally:
        stream.disconnect()


def test_disconnect_joins_thread_and_calls_close():
    closed = {"called": False}
    def close(handle):
        closed["called"] = True
    stream = PollingSensorStream(
        "imu", read=lambda h: {"x": 1},
        open=lambda: "h", close=close, hz=1000,
    )
    stream.connect()
    stream.disconnect()
    assert closed["called"] is True
    assert stream._handle is None
    assert not stream._thread.is_alive()


def test_disconnect_without_close_callback():
    stream = PollingSensorStream("imu", read=lambda: {"x": 1}, hz=1000)
    stream.connect()
    stream.disconnect()


def test_open_raises_bubbles_out_of_connect():
    def open_dev():
        raise RuntimeError("permission denied")
    stream = PollingSensorStream(
        "imu", read=lambda h: {"x": 1}, open=open_dev, hz=100,
    )
    with pytest.raises(RuntimeError, match="permission denied"):
        stream.connect()


def test_close_raises_emits_warning_but_disconnect_succeeds():
    health: list = []
    def close(handle):
        raise RuntimeError("close failed")
    stream = PollingSensorStream(
        "imu", read=lambda h: {"x": 1},
        open=lambda: "h", close=close, hz=1000,
    )
    stream.on_health(health.append)
    stream.connect()
    stream.disconnect()
    warnings = [h for h in health if h.kind == HealthEventKind.WARNING]
    assert any("close failed" in (w.detail or "") for w in warnings)


def test_start_recording_flips_writing_flag():
    stream = PollingSensorStream("imu", read=lambda: {"x": 1}, hz=1000)
    stream.connect()
    try:
        stream.start_recording(_clock())
        assert stream._writing is True
    finally:
        stream.stop_recording()
        stream.disconnect()


def test_stop_recording_returns_finalization_report():
    stream = PollingSensorStream("imu", read=lambda: {"x": 1}, hz=1000)
    stream.connect()
    stream.start_recording(_clock())
    time.sleep(0.05)
    report = stream.stop_recording()
    stream.disconnect()
    assert report.stream_id == "imu"
    assert report.status == "completed"
    assert report.frame_count > 0
    assert report.file_path is None
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


# ---------------------------------------------------------------------------
# Intra-host sync anchor
# ---------------------------------------------------------------------------

class TestRecordingAnchor:
    """Per-recording-window intra-host sync anchor capture.

    PollingSensorStream has no device clock — ``first_frame_device_ns``
    must always be ``None``, while ``armed_host_ns`` and
    ``first_frame_host_ns`` are populated on the first recorded sample.
    """

    def test_polling_sensor_anchor_captured_without_device_ts(self):
        """Polling sensor has no device clock — anchor captured with
        first_frame_device_ns=None."""
        stream = PollingSensorStream(
            "imu", read=lambda: {"x": 1.0}, hz=1000,
        )
        stream.connect()
        armed_ns = time.monotonic_ns()
        clock = SessionClock(
            sync_point=SyncPoint.create_now("h"),
            recording_armed_ns=armed_ns,
        )
        stream.start_recording(clock)
        # Let the capture thread produce at least one recorded sample.
        time.sleep(0.05)
        report = stream.stop_recording()
        stream.disconnect()

        assert report.recording_anchor is not None
        assert report.recording_anchor.armed_host_ns == armed_ns
        assert report.recording_anchor.first_frame_host_ns >= armed_ns
        # KEY: polling sensors have no device clock.
        assert report.recording_anchor.first_frame_device_ns is None
