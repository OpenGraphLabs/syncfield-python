"""Multi-producer stress test for PushSensorStream."""

from __future__ import annotations

import threading
import time

import pytest

from syncfield.adapters.push_sensor import PushSensorStream
from syncfield.types import SampleEvent

pytestmark = pytest.mark.slow


def test_concurrent_pushers_no_lost_samples():
    N_THREADS = 50
    PUSHES_PER_THREAD = 100
    EXPECTED_TOTAL = N_THREADS * PUSHES_PER_THREAD

    samples: list[SampleEvent] = []
    stream = PushSensorStream("ble")
    stream.on_sample(samples.append)
    stream.connect()
    stream._writing = True  # simulate recording state

    def producer(tid: int) -> None:
        for i in range(PUSHES_PER_THREAD):
            stream.push({"tid": tid, "i": i}, capture_ns=tid * 1_000_000 + i)

    threads = [threading.Thread(target=producer, args=(t,)) for t in range(N_THREADS)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0

    stream._writing = False
    stream.disconnect()

    assert len(samples) == EXPECTED_TOTAL, (
        f"expected {EXPECTED_TOTAL}, got {len(samples)}"
    )

    # Frame numbers are unique and cover [0, EXPECTED_TOTAL)
    fnums = sorted(s.frame_number for s in samples)
    assert fnums == list(range(EXPECTED_TOTAL))

    # Recorded count matches
    assert stream._write_core.recorded_count == EXPECTED_TOTAL

    print(f"  pushed {EXPECTED_TOTAL} samples in {elapsed:.3f}s "
          f"({EXPECTED_TOTAL / elapsed:.0f} samples/sec)")


def test_push_during_writing_toggle_does_not_crash():
    """A user thread that pushes right as _writing toggles must not crash."""
    stream = PushSensorStream("ble")
    stream.connect()
    stream._writing = True

    stop_event = threading.Event()
    pushed = [0]

    def producer():
        while not stop_event.is_set():
            stream.push({"x": pushed[0]})
            pushed[0] += 1

    t = threading.Thread(target=producer)
    t.start()
    time.sleep(0.1)

    # Toggle writing off (simulates stop_recording)
    stream._writing = False
    time.sleep(0.05)
    stop_event.set()
    t.join(timeout=1.0)
    stream.disconnect()

    assert pushed[0] > 0
