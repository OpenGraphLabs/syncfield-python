import time

from syncfield.health import HealthSystem, Severity
from syncfield.health.detector import DetectorBase
from syncfield.types import HealthEvent, HealthEventKind, SampleEvent, SessionState


class Custom(DetectorBase):
    name = "custom"
    default_severity = Severity.WARNING


def test_health_system_boots_and_accepts_inputs():
    hs = HealthSystem()
    hs.start()
    try:
        hs.observe_sample("cam", SampleEvent(stream_id="cam", frame_number=1, capture_ns=1))
        hs.observe_health("cam", HealthEvent(
            stream_id="cam", kind=HealthEventKind.WARNING, at_ns=1,
            severity=Severity.WARNING, source="test", fingerprint="cam:adapter:test",
        ))
        hs.observe_state(SessionState.IDLE, SessionState.CONNECTED)
    finally:
        hs.stop()


def test_health_system_register_and_unregister():
    hs = HealthSystem()
    d = Custom()
    hs.register(d)
    assert any(x.name == "custom" for x in hs.iter_detectors())
    hs.unregister("custom")
    assert not any(x.name == "custom" for x in hs.iter_detectors())


def test_health_system_installs_default_detectors():
    hs = HealthSystem()
    names = {d.name for d in hs.iter_detectors()}
    for expected in (
        "adapter",
        "stream-stall",
        "fps-drop",
        "jitter",
        "startup-failure",
        "backpressure",
    ):
        assert expected in names, f"missing default detector: {expected}"


def test_health_system_callbacks_fire_on_open_and_close():
    hs = HealthSystem(passthrough_close_ns=1)  # close instantly for the test
    opened, closed = [], []
    hs.on_incident_opened = opened.append
    hs.on_incident_closed = closed.append

    hs.start()
    try:
        hs.observe_health("cam", HealthEvent(
            stream_id="cam", kind=HealthEventKind.ERROR, at_ns=1,
            severity=Severity.ERROR, source="adapter:test",
            fingerprint="cam:adapter:xlink-error",
        ))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if opened and closed:
                break
            time.sleep(0.02)
    finally:
        hs.stop()
    assert opened, "incident was not opened"
    assert closed, "incident was not closed"
