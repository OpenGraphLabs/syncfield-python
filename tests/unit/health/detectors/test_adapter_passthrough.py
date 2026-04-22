from syncfield.health.detectors.adapter_passthrough import AdapterEventPassthrough
from syncfield.health.severity import Severity
from syncfield.health.types import Incident
from syncfield.types import HealthEvent, HealthEventKind


def _adapter_ev(at_ns: int) -> HealthEvent:
    return HealthEvent(
        stream_id="cam",
        kind=HealthEventKind.ERROR,
        at_ns=at_ns,
        detail="x",
        severity=Severity.ERROR,
        source="adapter:oak",
        fingerprint="cam:adapter:xlink-error",
    )


def test_tick_emits_nothing():
    d = AdapterEventPassthrough()
    assert list(d.tick(now_ns=1000)) == []


def test_close_condition_respects_quiet_window():
    d = AdapterEventPassthrough(quiet_ns=500)
    inc = Incident.opened_from(_adapter_ev(100), title="x")
    assert d.close_condition(inc, now_ns=400) is False   # 300 < 500
    inc.record_event(_adapter_ev(900))
    assert d.close_condition(inc, now_ns=1000) is False  # 100 < 500
    assert d.close_condition(inc, now_ns=1500) is True   # 600 >= 500
