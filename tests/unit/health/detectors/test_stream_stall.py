from syncfield.health.detectors.stream_stall import StreamStallDetector
from syncfield.health.types import Incident
from syncfield.types import SampleEvent


def _sample(stream_id: str, capture_ns: int) -> SampleEvent:
    return SampleEvent(stream_id=stream_id, frame_number=1, capture_ns=capture_ns)


def _connect(d: StreamStallDetector, stream_id: str, at_ns: int = 0) -> None:
    """Test helper — flip the stream into ``connected`` state.

    The detector only tracks streams currently in the ``connected``
    connection state, so production callers (the orchestrator) must
    invoke ``observe_connection_state`` to register a stream. Unit
    tests do the same explicitly.
    """
    d.observe_connection_state(stream_id, "connected", at_ns=at_ns)


def test_no_fire_before_seeing_any_sample():
    d = StreamStallDetector(stall_threshold_ns=1000)
    assert list(d.tick(now_ns=10_000)) == []


def test_no_fire_for_streams_never_connected():
    """Samples for streams that never reported 'connected' are ignored."""
    d = StreamStallDetector(stall_threshold_ns=1000)
    d.observe_sample("cam", _sample("cam", capture_ns=100))
    assert list(d.tick(now_ns=2000)) == []


def test_fires_when_silent_longer_than_threshold():
    d = StreamStallDetector(stall_threshold_ns=1000)
    _connect(d, "cam", at_ns=0)
    d.observe_sample("cam", _sample("cam", capture_ns=100))
    events = list(d.tick(now_ns=2000))   # 1900 ns of silence
    assert len(events) == 1
    ev = events[0]
    assert ev.stream_id == "cam"
    assert ev.fingerprint == "cam:stream-stall"
    assert ev.source == "detector:stream-stall"
    assert "silence" in (ev.detail or "").lower()


def test_does_not_refire_while_still_stalled():
    d = StreamStallDetector(stall_threshold_ns=1000)
    _connect(d, "cam", at_ns=0)
    d.observe_sample("cam", _sample("cam", capture_ns=100))
    fired_once = list(d.tick(now_ns=2000))
    fired_twice = list(d.tick(now_ns=3000))
    assert len(fired_once) == 1
    assert len(fired_twice) == 0


def test_refires_after_recovery_then_new_stall():
    d = StreamStallDetector(stall_threshold_ns=1000, recovery_ns=500)
    _connect(d, "cam", at_ns=0)
    d.observe_sample("cam", _sample("cam", capture_ns=0))
    list(d.tick(now_ns=2000))                          # fires stall

    # recovery: samples flow for ≥ recovery_ns
    for t in range(3000, 4100, 100):
        d.observe_sample("cam", _sample("cam", capture_ns=t))
    # Silence again.
    new_events = list(d.tick(now_ns=6000))
    assert len(new_events) == 1   # second stall → new event


def test_close_condition_requires_recent_sample_flow():
    d = StreamStallDetector(stall_threshold_ns=1000, recovery_ns=500)
    _connect(d, "cam", at_ns=0)
    d.observe_sample("cam", _sample("cam", capture_ns=0))
    events = list(d.tick(now_ns=2000))
    inc = Incident.opened_from(events[0], title="x")

    # Still silent — do not close.
    assert d.close_condition(inc, now_ns=2500) is False

    # Samples arrive across a 600 ns window → recovery_ns=500 satisfied.
    d.observe_sample("cam", _sample("cam", capture_ns=2600))
    d.observe_sample("cam", _sample("cam", capture_ns=3200))
    assert d.close_condition(inc, now_ns=3300) is True


def test_per_stream_independent_state():
    d = StreamStallDetector(stall_threshold_ns=1000)
    _connect(d, "a", at_ns=0)
    _connect(d, "b", at_ns=0)
    d.observe_sample("a", _sample("a", capture_ns=100))
    d.observe_sample("b", _sample("b", capture_ns=100))
    # stream b stays alive
    d.observe_sample("b", _sample("b", capture_ns=1800))
    events = list(d.tick(now_ns=2500))
    assert len(events) == 1
    assert events[0].stream_id == "a"


def test_disconnect_clears_tracking_no_post_teardown_fire():
    """Streams that disconnect should not raise stall incidents.

    This is the regression case for the noisy 'silence 10.5s' events
    seen at the end of every recording: when a stream tears down, its
    samples stop, but the silence is expected, not an incident.
    """
    d = StreamStallDetector(stall_threshold_ns=1000)
    _connect(d, "cam", at_ns=0)
    d.observe_sample("cam", _sample("cam", capture_ns=100))
    # Stream disconnects (e.g. as part of session.disconnect()).
    d.observe_connection_state("cam", "disconnected", at_ns=200)
    # Plenty of silence after disconnect — should NOT fire.
    assert list(d.tick(now_ns=10_000)) == []


def test_close_condition_resolves_when_stream_disconnects():
    """If an incident is open and the stream disconnects, the incident
    should auto-close — its silence is no longer this detector's
    concern."""
    d = StreamStallDetector(stall_threshold_ns=1000, recovery_ns=500)
    _connect(d, "cam", at_ns=0)
    d.observe_sample("cam", _sample("cam", capture_ns=0))
    events = list(d.tick(now_ns=2000))
    inc = Incident.opened_from(events[0], title="x")

    d.observe_connection_state("cam", "disconnected", at_ns=2100)
    assert d.close_condition(inc, now_ns=2500) is True


def test_reconnect_does_not_immediately_fire():
    """A stream that reconnects starts a fresh threshold window from
    the connect time, not from a stale capture_ns left over."""
    d = StreamStallDetector(stall_threshold_ns=1000)
    _connect(d, "cam", at_ns=0)
    d.observe_sample("cam", _sample("cam", capture_ns=100))
    d.observe_connection_state("cam", "disconnected", at_ns=200)
    # Long gap before reconnect — should not poison the next session.
    _connect(d, "cam", at_ns=10_000)
    # Just past threshold from old capture but well before threshold
    # from reconnect time — should NOT fire.
    assert list(d.tick(now_ns=10_500)) == []
    # Past threshold from reconnect with no samples — fires.
    events = list(d.tick(now_ns=12_000))
    assert len(events) == 1
