from pathlib import Path
from syncfield.types import FinalizationReport


def test_finalization_report_accepts_pending_aggregation_status():
    report = FinalizationReport(
        stream_id="overhead",
        status="pending_aggregation",
        frame_count=0,
        file_path=None,
        first_sample_at_ns=None,
        last_sample_at_ns=None,
        health_events=[],
        error=None,
    )
    assert report.status == "pending_aggregation"


def test_finalization_report_incidents_default_empty():
    from syncfield.types import FinalizationReport
    r = FinalizationReport(
        stream_id="cam", status="completed", frame_count=10, file_path=None,
        first_sample_at_ns=0, last_sample_at_ns=100, health_events=[], error=None,
    )
    assert r.incidents == []


def test_finalization_report_accepts_incidents():
    from syncfield.health.types import Incident
    from syncfield.health.severity import Severity
    from syncfield.types import FinalizationReport, HealthEvent, HealthEventKind

    ev = HealthEvent(
        stream_id="cam", kind=HealthEventKind.ERROR, at_ns=1, detail="x",
        severity=Severity.ERROR, source="detector:stream-stall",
        fingerprint="cam:stream-stall",
    )
    inc = Incident.opened_from(ev, title="stall")
    r = FinalizationReport(
        stream_id="cam", status="completed", frame_count=10, file_path=None,
        first_sample_at_ns=0, last_sample_at_ns=100, health_events=[], error=None,
        incidents=[inc],
    )
    assert r.incidents == [inc]
