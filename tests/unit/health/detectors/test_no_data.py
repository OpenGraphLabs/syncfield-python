from syncfield.health.detectors.no_data import NoDataDetector
from syncfield.health.types import Incident
from syncfield.types import SampleEvent


def _s(stream: str, t_ns: int) -> SampleEvent:
    return SampleEvent(stream_id=stream, frame_number=0, capture_ns=t_ns)


def test_no_fire_before_threshold():
    d = NoDataDetector(threshold_ns=1000)
    d.observe_connection_state("cam", "connected", at_ns=100)
    assert list(d.tick(now_ns=500)) == []   # 400 ns elapsed, under 1000


def test_fires_after_threshold_without_sample():
    d = NoDataDetector(threshold_ns=1000)
    d.observe_connection_state("cam", "connected", at_ns=100)
    out = list(d.tick(now_ns=2000))          # 1900 ns elapsed
    assert len(out) == 1
    ev = out[0]
    assert ev.stream_id == "cam"
    assert ev.fingerprint == "cam:no-data"
    assert ev.source == "detector:no-data"
    assert "no data" in (ev.detail or "").lower()


def test_does_not_refire_while_still_no_data():
    d = NoDataDetector(threshold_ns=1000)
    d.observe_connection_state("cam", "connected", at_ns=100)
    first = list(d.tick(now_ns=2000))
    second = list(d.tick(now_ns=3000))
    assert len(first) == 1
    assert len(second) == 0


def test_close_condition_satisfied_once_sample_arrives():
    d = NoDataDetector(threshold_ns=1000)
    d.observe_connection_state("cam", "connected", at_ns=100)
    events = list(d.tick(now_ns=2000))
    inc = Incident.opened_from(events[0], title="x")

    assert d.close_condition(inc, now_ns=2100) is False   # still no sample

    d.observe_sample("cam", _s("cam", 2200))
    assert d.close_condition(inc, now_ns=2300) is True


def test_resets_bookkeeping_on_non_connected_state():
    d = NoDataDetector(threshold_ns=1000)
    d.observe_connection_state("cam", "connected", at_ns=100)
    list(d.tick(now_ns=2000))  # fires

    d.observe_connection_state("cam", "failed", at_ns=2500)
    # Back to connected → fresh clock, no duplicate fire.
    d.observe_connection_state("cam", "connected", at_ns=3000)
    assert list(d.tick(now_ns=3500)) == []   # only 500 ns since new connected


def test_per_stream_independent_state():
    d = NoDataDetector(threshold_ns=1000)
    d.observe_connection_state("a", "connected", at_ns=100)
    d.observe_connection_state("b", "connected", at_ns=100)
    d.observe_sample("b", _s("b", 200))

    out = list(d.tick(now_ns=2000))
    assert len(out) == 1
    assert out[0].stream_id == "a"
