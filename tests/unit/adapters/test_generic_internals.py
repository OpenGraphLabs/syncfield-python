"""Tests for syncfield.adapters._generic internals."""

from __future__ import annotations

import threading

from syncfield.adapters._generic import (
    _SensorWriteCore,
    _default_sensor_capabilities,
    _resolve_capabilities,
)
from syncfield.types import StreamCapabilities


# ---------------------------------------------------------------------------
# _SensorWriteCore: frame counter
# ---------------------------------------------------------------------------

def test_sensor_write_core_frame_counter_starts_at_zero():
    core = _SensorWriteCore("imu")
    assert core.next_frame_number() == 0
    assert core.next_frame_number() == 1
    assert core.next_frame_number() == 2


def test_sensor_write_core_frame_counter_is_thread_safe():
    """100 threads each calling next_frame_number 50 times → 5000 unique values."""
    core = _SensorWriteCore("imu")
    results: list[int] = []
    lock = threading.Lock()

    def worker():
        for _ in range(50):
            n = core.next_frame_number()
            with lock:
                results.append(n)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 5000
    assert len(set(results)) == 5000


# ---------------------------------------------------------------------------
# _SensorWriteCore: record_sample + timing
# ---------------------------------------------------------------------------

def test_sensor_write_core_recorded_count_starts_at_zero():
    core = _SensorWriteCore("imu")
    assert core.recorded_count == 0


def test_sensor_write_core_record_sample_increments_count():
    core = _SensorWriteCore("imu")
    core.record_sample(1000)
    core.record_sample(2000)
    core.record_sample(3000)
    assert core.recorded_count == 3


def test_sensor_write_core_record_sample_tracks_first_and_last_ns():
    core = _SensorWriteCore("imu")
    core.record_sample(1000)
    core.record_sample(2500)
    core.record_sample(3700)
    assert core.first_sample_at_ns == 1000
    assert core.last_sample_at_ns == 3700


def test_sensor_write_core_first_last_are_none_before_any_sample():
    core = _SensorWriteCore("imu")
    assert core.first_sample_at_ns is None
    assert core.last_sample_at_ns is None


def test_sensor_write_core_record_sample_is_thread_safe():
    """200 threads each recording 25 samples → 5000 total recorded."""
    core = _SensorWriteCore("imu")

    def worker(start_ns: int):
        for i in range(25):
            core.record_sample(start_ns + i)

    threads = [threading.Thread(target=worker, args=(t * 10000,)) for t in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert core.recorded_count == 5000


# ---------------------------------------------------------------------------
# _SensorWriteCore: reset_recording_stats
# ---------------------------------------------------------------------------

def test_reset_recording_stats_clears_count_and_timing():
    core = _SensorWriteCore("imu")
    core.record_sample(1000)
    core.record_sample(2000)
    assert core.recorded_count == 2

    core.reset_recording_stats()
    assert core.recorded_count == 0
    assert core.first_sample_at_ns is None
    assert core.last_sample_at_ns is None


def test_reset_recording_stats_does_not_affect_frame_counter():
    core = _SensorWriteCore("imu")
    core.next_frame_number()
    core.next_frame_number()
    core.record_sample(1000)
    core.reset_recording_stats()
    # frame counter continues from where it left off
    assert core.next_frame_number() == 2


def test_reset_then_record_again_works():
    core = _SensorWriteCore("imu")
    core.record_sample(100)
    core.reset_recording_stats()
    core.record_sample(500)
    core.record_sample(600)
    assert core.recorded_count == 2
    assert core.first_sample_at_ns == 500
    assert core.last_sample_at_ns == 600


# ---------------------------------------------------------------------------
# _default_sensor_capabilities
# ---------------------------------------------------------------------------

def test_default_sensor_capabilities_precise_true():
    caps = _default_sensor_capabilities(precise=True)
    assert caps.provides_audio_track is False
    assert caps.supports_precise_timestamps is True
    assert caps.is_removable is False
    assert caps.produces_file is False


def test_default_sensor_capabilities_precise_false():
    caps = _default_sensor_capabilities(precise=False)
    assert caps.supports_precise_timestamps is False
    assert caps.produces_file is False


def test_resolve_capabilities_returns_default_when_user_none():
    caps = _resolve_capabilities(None, precise=True)
    assert caps == _default_sensor_capabilities(precise=True)


def test_resolve_capabilities_returns_user_value_when_provided():
    user = StreamCapabilities(
        provides_audio_track=False,
        supports_precise_timestamps=False,
        is_removable=True,
        produces_file=True,
    )
    caps = _resolve_capabilities(user, precise=True)
    assert caps is user
