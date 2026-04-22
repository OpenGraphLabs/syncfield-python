from syncfield.health.detectors.backpressure import BackpressureDetector
from syncfield.health.types import Incident, WriterStats


def _stat(at_ns, depth, cap=16, dropped=0):
    return WriterStats(stream_id="cam", at_ns=at_ns, queue_depth=depth, queue_capacity=cap, dropped=dropped)


def test_does_not_fire_with_normal_fullness():
    d = BackpressureDetector()
    for t in range(0, int(3e9), int(2.5e8)):
        d.observe_writer_stats("cam", _stat(t, depth=2))
    assert list(d.tick(now_ns=int(3e9))) == []


def test_fires_when_queue_sustained_above_threshold():
    d = BackpressureDetector(fullness_threshold=0.8, sustain_ns=int(2e9))
    for t in range(0, int(3e9), int(2.5e8)):
        d.observe_writer_stats("cam", _stat(t, depth=14))   # 14/16 = 0.875
    emitted = list(d.tick(now_ns=int(3e9)))
    assert len(emitted) == 1
    assert emitted[0].fingerprint == "cam:backpressure"


def test_fires_on_any_drop_increment():
    d = BackpressureDetector()
    d.observe_writer_stats("cam", _stat(0, depth=1, dropped=0))
    d.observe_writer_stats("cam", _stat(int(1e8), depth=1, dropped=5))
    emitted = list(d.tick(now_ns=int(2e8)))
    assert len(emitted) == 1


def test_close_condition_requires_low_and_no_new_drops():
    d = BackpressureDetector(fullness_threshold=0.8, sustain_ns=int(2e9),
                             recovery_ratio=0.3, recovery_ns=int(1e9))
    for t in range(0, int(3e9), int(2.5e8)):
        d.observe_writer_stats("cam", _stat(t, depth=14))
    events = list(d.tick(now_ns=int(3e9)))
    inc = Incident.opened_from(events[0], title="x")

    # Recovery in progress.
    d.observe_writer_stats("cam", _stat(int(3.5e9), depth=2))
    assert d.close_condition(inc, now_ns=int(4e9)) is False   # only 500 ms of recovery

    d.observe_writer_stats("cam", _stat(int(5e9), depth=2))
    assert d.close_condition(inc, now_ns=int(5e9)) is True    # 1.5 s of recovery
