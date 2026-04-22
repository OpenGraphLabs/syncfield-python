import dataclasses

from syncfield.health.severity import Severity
from syncfield.health.types import Incident, IncidentSnapshot
from syncfield.types import HealthEvent, HealthEventKind
from syncfield.viewer.state import SessionSnapshot, StreamSnapshot


def _ev(at_ns: int) -> HealthEvent:
    return HealthEvent(
        stream_id="cam", kind=HealthEventKind.ERROR, at_ns=at_ns, detail="x",
        severity=Severity.ERROR, source="detector:stream-stall",
        fingerprint="cam:stream-stall",
    )


def test_session_snapshot_has_incident_fields():
    snap = SessionSnapshot(
        host_id="h", state="recording", output_dir="/tmp",
        sync_point_monotonic_ns=None, sync_point_wall_clock_ns=None,
        chirp_start_ns=None, chirp_stop_ns=None, chirp_enabled=False,
        elapsed_s=0.0, streams={}, active_incidents=[], resolved_incidents=[],
    )
    assert snap.active_incidents == []
    assert snap.resolved_incidents == []


def test_stream_snapshot_no_longer_has_health_count():
    fields = {f.name for f in dataclasses.fields(StreamSnapshot)}
    assert "health_count" not in fields
    assert "problem_count" not in fields


def test_session_snapshot_no_longer_has_health_log():
    fields = {f.name for f in dataclasses.fields(SessionSnapshot)}
    assert "health_log" not in fields
