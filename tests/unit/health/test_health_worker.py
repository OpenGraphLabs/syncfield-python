import threading
import time

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.tracker import IncidentTracker
from syncfield.health.types import WriterStats
from syncfield.health.worker import HealthWorker
from syncfield.types import HealthEvent, HealthEventKind, SampleEvent, SessionState


class RecordingDetector(DetectorBase):
    name = "recorder"
    default_severity = Severity.INFO

    def __init__(self) -> None:
        self.samples = []
        self.healths = []
        self.states = []
        self.writer_stats = []
        self.ticks = 0

    def observe_sample(self, stream_id, sample):
        self.samples.append((stream_id, sample.capture_ns))

    def observe_health(self, stream_id, event):
        self.healths.append((stream_id, event.at_ns))

    def observe_state(self, old, new):
        self.states.append((old, new))

    def observe_writer_stats(self, stream_id, stats):
        self.writer_stats.append((stream_id, stats.queue_depth))

    def tick(self, now_ns):
        self.ticks += 1
        return iter(())


def _wait_until(pred, timeout=1.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def test_worker_drains_all_ingress_queues_on_tick():
    tr = IncidentTracker()
    det = RecordingDetector()
    tr.bind_detector(det)
    w = HealthWorker(tracker=tr, detectors=[det], tick_hz=100)

    w.start()
    try:
        w.push_sample("cam", SampleEvent(stream_id="cam", frame_number=1, capture_ns=42))
        # IMPORTANT: health events must now carry a non-empty fingerprint because
        # Task 6 added a guard in IncidentTracker.ingest. Provide one.
        w.push_health("cam", HealthEvent(
            stream_id="cam", kind=HealthEventKind.WARNING, at_ns=1,
            severity=Severity.WARNING, source="test", fingerprint="cam:test:ingest",
        ))
        w.push_state(SessionState.IDLE, SessionState.CONNECTED)
        w.push_writer_stats("cam", WriterStats("cam", 1, 2, 16, 0))

        assert _wait_until(lambda: det.samples and det.healths and det.states and det.writer_stats)
    finally:
        w.stop()

    assert det.samples[0] == ("cam", 42)
    assert det.healths[0] == ("cam", 1)
    assert det.states[0] == (SessionState.IDLE, SessionState.CONNECTED)
    assert det.writer_stats[0] == ("cam", 2)


def test_worker_ticks_at_roughly_configured_rate():
    tr = IncidentTracker()
    det = RecordingDetector()
    w = HealthWorker(tracker=tr, detectors=[det], tick_hz=50)
    w.start()
    try:
        time.sleep(0.2)  # ~10 ticks
    finally:
        w.stop()
    # Loose bound to avoid flakiness under loaded CI.
    assert det.ticks >= 5


def test_worker_feeds_detector_tick_output_into_tracker():
    class EmitsOneAndDone(DetectorBase):
        name = "emit"
        default_severity = Severity.WARNING

        def __init__(self):
            self.fired = False

        def tick(self, now_ns):
            if self.fired:
                return iter(())
            self.fired = True
            return iter([HealthEvent(
                stream_id="cam", kind=HealthEventKind.WARNING, at_ns=now_ns,
                detail="synthetic", severity=Severity.WARNING,
                source="detector:emit", fingerprint="cam:emit",
            )])

        def close_condition(self, inc, now_ns):
            return False

    tr = IncidentTracker()
    det = EmitsOneAndDone()
    tr.bind_detector(det)
    w = HealthWorker(tracker=tr, detectors=[det], tick_hz=100)
    w.start()
    try:
        assert _wait_until(lambda: len(tr.open_incidents()) == 1)
    finally:
        w.stop()


def test_worker_stop_is_idempotent():
    tr = IncidentTracker()
    det = RecordingDetector()
    w = HealthWorker(tracker=tr, detectors=[det], tick_hz=50)
    w.start()
    w.stop()
    w.stop()  # does not raise


def test_worker_drains_connection_state_queue_and_fans_out():
    class Spy(DetectorBase):
        name = "conn-spy"
        default_severity = Severity.INFO

        def __init__(self):
            self.calls = []

        def observe_connection_state(self, stream_id, new_state, at_ns):
            self.calls.append((stream_id, new_state, at_ns))

    tr = IncidentTracker()
    spy = Spy()
    w = HealthWorker(tracker=tr, detectors=[spy], tick_hz=100)
    w.start()
    try:
        w.push_connection_state("cam", "connecting", 1)
        w.push_connection_state("cam", "connected", 2)
        assert _wait_until(lambda: len(spy.calls) == 2)
    finally:
        w.stop()

    assert spy.calls == [("cam", "connecting", 1), ("cam", "connected", 2)]
