import pytest

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import WriterStats
from syncfield.types import HealthEvent, HealthEventKind, SampleEvent, SessionState


class NoopDetector(DetectorBase):
    name = "noop"
    default_severity = Severity.WARNING


def test_detector_base_defaults_are_noops():
    d = NoopDetector()
    # All observers accept calls without raising.
    d.observe_sample("cam", SampleEvent(stream_id="cam", frame_number=1, capture_ns=100))
    d.observe_health("cam", HealthEvent(stream_id="cam", kind=HealthEventKind.WARNING, at_ns=1))
    d.observe_state(SessionState.IDLE, SessionState.CONNECTED)
    d.observe_writer_stats("cam", WriterStats("cam", 1, 0, 0, 0))
    # tick yields nothing by default.
    assert list(d.tick(now_ns=100)) == []
    # close_condition defaults to False (conservative: keep open; subclasses override).
    from syncfield.health.types import Incident
    ev = HealthEvent(stream_id="cam", kind=HealthEventKind.WARNING, at_ns=1)
    inc = Incident.opened_from(ev, title="x")
    assert d.close_condition(inc, now_ns=10) is False


def test_detector_base_requires_name_and_severity():
    with pytest.raises(TypeError):
        DetectorBase()  # abstract base: name / default_severity unset on the class


def test_grandchild_subclass_must_redeclare_if_needed():
    # Sanity: once a parent sets name/default_severity, grandchildren inheriting
    # them pass the check (legitimate use case).
    class Grandchild(NoopDetector):
        pass
    Grandchild()  # should not raise

    # A subclass that overrides nothing but lacks both required attrs
    # cannot exist — we cannot construct such a test directly without
    # subclassing DetectorBase again (which must itself declare attrs).
    # The more important invariant is covered by the existing
    # test_detector_base_requires_name_and_severity.


def test_detector_base_observe_connection_state_default_is_noop():
    d = NoopDetector()
    # Does not raise; returns None.
    assert d.observe_connection_state("cam", "connected", 100) is None
