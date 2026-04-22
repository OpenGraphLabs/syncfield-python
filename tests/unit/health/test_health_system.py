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
    hs.on_incident_opened(opened.append)
    hs.on_incident_closed(closed.append)

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


def test_health_system_double_start_is_idempotent():
    hs = HealthSystem()
    hs.start()
    first_worker = hs._worker
    hs.start()   # should not spin up a new thread
    second_worker = hs._worker
    try:
        assert first_worker is second_worker, "start() should be idempotent"
    finally:
        hs.stop()


def test_health_system_register_after_start_warns():
    import warnings

    hs = HealthSystem()
    hs.start()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            hs.register(Custom())
            assert any(issubclass(w.category, RuntimeWarning) for w in caught)
            assert any("after HealthSystem.start()" in str(w.message) for w in caught)
    finally:
        hs.stop()


def test_register_stream_propagates_target_hz_to_detectors():
    hs = HealthSystem()
    hs.register_stream("cam", 30.0)

    fps = next(d for d in hs.iter_detectors() if d.name == "fps-drop")
    jitter = next(d for d in hs.iter_detectors() if d.name == "jitter")

    assert fps._target_getter("cam") == 30.0
    assert jitter._target_getter("cam") == 30.0
    assert fps._target_getter("unknown") is None


def test_health_system_observe_connection_state_routes_to_worker():
    import time as _time
    class Spy(DetectorBase):
        name = "conn-spy"
        default_severity = Severity.INFO

        def __init__(self):
            self.calls = []

        def observe_connection_state(self, stream_id, new_state, at_ns):
            self.calls.append((stream_id, new_state, at_ns))

    hs = HealthSystem()
    spy = Spy()
    hs.register(spy)

    hs.start()
    try:
        hs.observe_connection_state("cam", "connecting", 10)
        hs.observe_connection_state("cam", "connected", 20)
        deadline = _time.monotonic() + 2.0
        while _time.monotonic() < deadline and len(spy.calls) < 2:
            _time.sleep(0.02)
    finally:
        hs.stop()
    assert spy.calls == [("cam", "connecting", 10), ("cam", "connected", 20)]
