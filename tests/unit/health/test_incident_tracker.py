from typing import List

import pytest

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.tracker import IncidentTracker
from syncfield.health.types import Incident
from syncfield.types import HealthEvent, HealthEventKind


def _ev(at_ns: int, fingerprint: str = "cam:stall", severity: Severity = Severity.ERROR,
        detail: str = "x") -> HealthEvent:
    return HealthEvent(
        stream_id="cam",
        kind=HealthEventKind.ERROR,
        at_ns=at_ns,
        detail=detail,
        severity=severity,
        source="detector:stream-stall",
        fingerprint=fingerprint,
    )


class AlwaysCloseAfter(DetectorBase):
    name = "stall"
    default_severity = Severity.ERROR

    def __init__(self, close_after_ns: int) -> None:
        self._close_after = close_after_ns

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        return now_ns - incident.last_event_at_ns >= self._close_after


def test_tracker_opens_incident_on_first_matching_event():
    tr = IncidentTracker()
    tr.bind_detector(AlwaysCloseAfter(close_after_ns=1000))
    opened: List[Incident] = []
    tr.on_opened = opened.append

    tr.ingest(_ev(100))

    assert len(tr.open_incidents()) == 1
    assert opened and opened[0].stream_id == "cam"


def test_tracker_groups_same_fingerprint_into_one_incident():
    tr = IncidentTracker()
    tr.bind_detector(AlwaysCloseAfter(close_after_ns=1_000_000_000))

    tr.ingest(_ev(100, severity=Severity.WARNING))
    tr.ingest(_ev(200, severity=Severity.ERROR))  # escalate
    tr.ingest(_ev(300, severity=Severity.ERROR))

    opens = tr.open_incidents()
    assert len(opens) == 1
    inc = opens[0]
    assert inc.event_count == 3
    assert inc.severity == Severity.ERROR
    assert inc.last_event_at_ns == 300


def test_tracker_closes_incident_when_detector_close_condition_fires():
    tr = IncidentTracker()
    tr.bind_detector(AlwaysCloseAfter(close_after_ns=500))
    closed: List[Incident] = []
    tr.on_closed = closed.append

    tr.ingest(_ev(100))
    tr.tick(now_ns=200)          # 100 ns since last event, not yet
    assert tr.resolved_incidents() == []

    tr.tick(now_ns=700)          # 600 ns since last event → close
    assert len(tr.resolved_incidents()) == 1
    assert tr.open_incidents() == []
    assert closed and closed[0].closed_at_ns == 700


def test_tracker_reopens_after_close_on_same_fingerprint():
    tr = IncidentTracker()
    tr.bind_detector(AlwaysCloseAfter(close_after_ns=100))

    tr.ingest(_ev(100))
    tr.tick(now_ns=500)           # closed
    assert tr.open_incidents() == []

    tr.ingest(_ev(1000))          # new incident, new id
    opens = tr.open_incidents()
    assert len(opens) == 1
    assert len(tr.resolved_incidents()) == 1
    assert opens[0].id != tr.resolved_incidents()[0].id


def test_tracker_unbound_fingerprint_falls_back_to_passthrough_close():
    # When an event arrives with a fingerprint whose detector is not bound,
    # the tracker still groups it, using the default passthrough close
    # window (30s of quiet).
    tr = IncidentTracker(passthrough_close_ns=500)

    tr.ingest(_ev(100, fingerprint="cam:adapter:xlink"))
    tr.tick(now_ns=400)
    assert tr.open_incidents()
    tr.tick(now_ns=1000)   # 900 ns since last event → closes
    assert tr.resolved_incidents()


def test_tracker_flush_callbacks_fire_on_update_too():
    tr = IncidentTracker()
    tr.bind_detector(AlwaysCloseAfter(close_after_ns=1_000_000_000))
    updates: List[Incident] = []
    tr.on_updated = updates.append

    tr.ingest(_ev(100))      # opens
    tr.ingest(_ev(200))      # updates
    tr.ingest(_ev(300))      # updates
    assert len(updates) == 2


def test_tracker_rejects_empty_fingerprint():
    tr = IncidentTracker()
    with pytest.raises(ValueError, match="fingerprint"):
        tr.ingest(HealthEvent(
            stream_id="cam",
            kind=HealthEventKind.ERROR,
            at_ns=1,
            # fingerprint defaults to ""
        ))
