from syncfield.health.detectors.jitter import JitterDetector
from syncfield.types import SampleEvent


def _s(t_ns: int) -> SampleEvent:
    return SampleEvent(stream_id="cam", frame_number=0, capture_ns=t_ns)


def test_steady_30hz_does_not_fire():
    d = JitterDetector(target_getter=lambda sid: 30.0)
    step = int(1e9 / 30)
    t = 0
    for _ in range(120):
        d.observe_sample("cam", _s(t))
        t += step
    assert list(d.tick(now_ns=t)) == []


def test_irregular_intervals_fire_when_p95_exceeds_ratio():
    d = JitterDetector(
        target_getter=lambda sid: 30.0,
        jitter_ratio=2.0,
        sustain_ns=500_000_000,
    )
    step = int(1e9 / 30)
    big = step * 4   # 4× target interval
    t = 0
    # 60 samples alternating between normal and 4× intervals.
    for i in range(60):
        d.observe_sample("cam", _s(t))
        t += big if i % 2 == 0 else step

    # Give sustain time to elapse with more irregular samples.
    for _ in range(20):
        d.observe_sample("cam", _s(t))
        t += big

    emitted = list(d.tick(now_ns=t + 500_000_000))
    assert len(emitted) == 1
    assert emitted[0].fingerprint == "cam:jitter"
