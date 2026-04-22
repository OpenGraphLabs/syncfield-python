from syncfield.health.severity import Severity
from syncfield.health.types import (
    Incident,
    IncidentArtifact,
    IncidentSnapshot,
    WriterStats,
)
from syncfield.types import HealthEvent, HealthEventKind


def _ev(at_ns: int, severity: Severity = Severity.ERROR) -> HealthEvent:
    return HealthEvent(
        stream_id="cam",
        kind=HealthEventKind.ERROR,
        at_ns=at_ns,
        detail="x",
        severity=severity,
        source="detector:stream-stall",
        fingerprint="cam:stream-stall",
    )


def test_writer_stats_fields():
    s = WriterStats(
        stream_id="cam",
        at_ns=100,
        queue_depth=3,
        queue_capacity=16,
        dropped=0,
    )
    assert s.queue_fullness == 3 / 16
    assert s.stream_id == "cam"


def test_writer_stats_zero_capacity_is_empty():
    s = WriterStats(stream_id="cam", at_ns=0, queue_depth=0, queue_capacity=0, dropped=0)
    assert s.queue_fullness == 0.0


def test_incident_from_first_event_initializes_fields():
    first = _ev(100)
    inc = Incident.opened_from(first, title="Stream stalled (silence 2.0s)")
    assert inc.stream_id == "cam"
    assert inc.fingerprint == "cam:stream-stall"
    assert inc.severity == Severity.ERROR
    assert inc.title == "Stream stalled (silence 2.0s)"
    assert inc.opened_at_ns == 100
    assert inc.closed_at_ns is None
    assert inc.event_count == 1
    assert inc.first_event == first
    assert inc.last_event == first
    assert inc.artifacts == []


def test_incident_record_event_escalates_severity_and_updates_last():
    inc = Incident.opened_from(_ev(100, severity=Severity.WARNING), title="t")
    inc.record_event(_ev(200, severity=Severity.ERROR))
    assert inc.event_count == 2
    assert inc.severity == Severity.ERROR
    assert inc.last_event.at_ns == 200
    assert inc.last_event_at_ns == 200


def test_incident_close():
    inc = Incident.opened_from(_ev(100), title="t")
    inc.close(at_ns=500)
    assert inc.closed_at_ns == 500
    assert inc.is_open is False


def test_incident_attach_artifact():
    inc = Incident.opened_from(_ev(100), title="t")
    inc.attach(IncidentArtifact(kind="crash_dump", path="/tmp/x.json"))
    assert inc.artifacts[0].kind == "crash_dump"
    assert inc.artifacts[0].path == "/tmp/x.json"


def test_incident_snapshot_shape():
    inc = Incident.opened_from(_ev(100), title="t")
    snap = IncidentSnapshot.from_incident(inc, now_ns=1_000_000_100)
    assert snap.id == inc.id
    assert snap.stream_id == "cam"
    assert snap.severity == "error"
    assert snap.is_open is True
    assert snap.ago_s >= 0


def test_incident_snapshot_uses_closed_at_as_anchor_when_closed():
    inc = Incident.opened_from(_ev(1_000_000_000), title="t")
    inc.record_event(_ev(1_100_000_000))   # last_event_at_ns = 1.1s
    inc.close(at_ns=1_500_000_000)          # closed 500ms later

    # now_ns is 2s after close
    snap = IncidentSnapshot.from_incident(inc, now_ns=3_500_000_000)
    assert snap.is_open is False
    assert snap.closed_at_ns == 1_500_000_000
    # ago_s is measured from closed_at_ns, not last_event_at_ns.
    assert abs(snap.ago_s - 2.0) < 0.01
