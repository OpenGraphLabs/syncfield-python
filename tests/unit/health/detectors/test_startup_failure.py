from syncfield.health.detectors.startup_failure import StartupFailureDetector
from syncfield.health.severity import Severity
from syncfield.types import HealthEvent, HealthEventKind


def _ev_for(phase: str, kind=HealthEventKind.ERROR) -> HealthEvent:
    return HealthEvent(
        stream_id="cam", kind=kind, at_ns=100, detail="boom",
        severity=Severity.ERROR, source="orchestrator",
        fingerprint=f"cam:adapter:startup-{phase}",
        data={"phase": phase},
    )


def test_fires_on_connect_phase_error():
    d = StartupFailureDetector()
    d.observe_health("cam", _ev_for("connect"))
    events = list(d.tick(now_ns=500))
    assert len(events) == 1
    assert events[0].fingerprint == "cam:startup-failure"
    assert events[0].data["phase"] == "connect"


def test_ignores_non_startup_phases():
    d = StartupFailureDetector()
    d.observe_health("cam", HealthEvent(
        stream_id="cam", kind=HealthEventKind.ERROR, at_ns=1, detail="x",
        severity=Severity.ERROR, source="adapter:foo", fingerprint="cam:adapter:xlink",
        data={},
    ))
    assert list(d.tick(now_ns=100)) == []


def test_closes_after_phase_success_signal():
    d = StartupFailureDetector()
    d.observe_health("cam", _ev_for("connect"))
    list(d.tick(now_ns=100))
    from syncfield.health.types import Incident
    inc = Incident.opened_from(_ev_for("connect"), title="x")

    # Before success, not closed.
    assert d.close_condition(inc, now_ns=200) is False

    # Success signal arrives.
    d.observe_health("cam", HealthEvent(
        stream_id="cam", kind=HealthEventKind.HEARTBEAT, at_ns=300, detail="connected",
        severity=Severity.INFO, source="orchestrator", fingerprint="cam:adapter:startup-success",
        data={"phase": "connect", "outcome": "success"},
    ))
    assert d.close_condition(inc, now_ns=400) is True
