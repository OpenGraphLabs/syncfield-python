"""Real-thread integration test for PollingSensorStream."""

from __future__ import annotations

import time

import pytest

from syncfield.adapters.polling_sensor import PollingSensorStream
from syncfield.types import SampleEvent


def test_real_thread_emits_samples_at_expected_rate():
    """Run capture thread at 200 Hz for 0.5s, expect ~100 samples +-30%."""
    counter = {"n": 0}

    def read():
        counter["n"] += 1
        return {"i": counter["n"]}

    samples: list[SampleEvent] = []
    stream = PollingSensorStream("imu", read=read, hz=200)
    stream.on_sample(samples.append)

    stream.connect()
    time.sleep(0.5)
    stream.disconnect()

    count = len(samples)
    assert 70 <= count <= 130, f"expected ~100 samples, got {count}"

    # Frame numbers are monotonic
    fnums = [s.frame_number for s in samples]
    assert fnums == sorted(fnums)
    assert fnums == list(range(len(fnums)))

    # capture_ns is monotonic
    caps = [s.capture_ns for s in samples]
    assert caps == sorted(caps)


def test_thread_keeps_running_across_writing_toggle():
    """Capture thread stays alive when _writing toggles."""
    stream = PollingSensorStream("imu", read=lambda: {"x": 1}, hz=200)
    stream.connect()
    try:
        assert stream._thread is not None and stream._thread.is_alive()
        stream._writing = True
        time.sleep(0.1)
        stream._writing = False
        time.sleep(0.1)
        assert stream._thread.is_alive()
    finally:
        stream.disconnect()
    assert not stream._thread.is_alive()


def test_disconnect_joins_thread_within_timeout():
    stream = PollingSensorStream("imu", read=lambda: {"x": 1}, hz=1000)
    stream.connect()
    t0 = time.monotonic()
    stream.disconnect()
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"disconnect took {elapsed:.3f}s, expected <1s"
    assert not stream._thread.is_alive()
