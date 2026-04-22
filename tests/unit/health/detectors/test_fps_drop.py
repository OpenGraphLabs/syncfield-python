from syncfield.health.detectors.fps_drop import FpsDropDetector
from syncfield.types import SampleEvent


def _s(stream: str, t_ns: int) -> SampleEvent:
    return SampleEvent(stream_id=stream, frame_number=0, capture_ns=t_ns)


def test_no_fire_if_fps_tracks_target():
    d = FpsDropDetector(target_getter=lambda sid: 30.0)
    # emit 30 samples over 1 second
    for i in range(30):
        d.observe_sample("cam", _s("cam", i * int(1e9 / 30)))
    assert list(d.tick(now_ns=int(1.1e9))) == []


def test_fires_when_observed_below_70_percent_for_3s():
    d = FpsDropDetector(
        target_getter=lambda sid: 30.0,
        drop_ratio=0.70,
        sustain_ns=3 * 1_000_000_000,
    )

    # 10 fps for 3.5 seconds — fps is 10, target 30, ratio 0.33.
    interval = int(1e9 / 10)
    t = 0
    while t <= int(3.5e9):
        d.observe_sample("cam", _s("cam", t))
        t += interval

    emitted = list(d.tick(now_ns=int(3.6e9)))
    assert len(emitted) == 1
    assert emitted[0].fingerprint == "cam:fps-drop"
    assert emitted[0].data["target_hz"] == 30.0
    assert emitted[0].data["observed_hz"] < 15.0


def test_does_not_fire_without_target_before_warmup():
    d = FpsDropDetector(
        target_getter=lambda sid: None,
        baseline_warmup_ns=5_000_000_000,
    )
    # 10 fps, but only for 1s — under warmup.
    t = 0
    for _ in range(10):
        d.observe_sample("cam", _s("cam", t))
        t += int(1e8)
    assert list(d.tick(now_ns=int(1.1e9))) == []


def test_learns_baseline_then_fires_on_subsequent_drop():
    d = FpsDropDetector(
        target_getter=lambda sid: None,
        baseline_warmup_ns=1_000_000_000,
        baseline_window_ns=2_000_000_000,
        drop_ratio=0.7,
        sustain_ns=1_000_000_000,
    )
    # 3 s @ 30 fps → baseline ≈ 30.
    t = 0
    while t <= int(3e9):
        d.observe_sample("cam", _s("cam", t))
        t += int(1e9 / 30)
    # 1.5 s of 10 fps → drop.
    end = t + int(1.5e9)
    while t <= end:
        d.observe_sample("cam", _s("cam", t))
        t += int(1e8)
    emitted = list(d.tick(now_ns=t))
    assert len(emitted) == 1
