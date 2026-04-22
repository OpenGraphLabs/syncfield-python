"""Integration: real SessionOrchestrator + FakeStream → incidents flow end-to-end."""
import json
import threading
import time
from pathlib import Path

import pytest

from syncfield.orchestrator import SessionOrchestrator
from syncfield.stream import StreamBase
from syncfield.clock import SessionClock
from syncfield.types import FinalizationReport, SampleEvent, StreamCapabilities


class FakeStream(StreamBase):
    def __init__(self, stream_id: str, target_hz: float | None = None):
        super().__init__(
            id=stream_id,
            kind="sensor",
            capabilities=StreamCapabilities(target_hz=target_hz),
        )
        self._interval = 1.0 / 30.0
        self._stop_thread = threading.Event()
        self._pause = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame = 0

    def connect(self):
        self._stop_thread.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def disconnect(self):
        self._stop_thread.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def start_recording(self, session_clock: SessionClock) -> None:
        pass

    def stop_recording(self) -> FinalizationReport:
        return FinalizationReport(
            stream_id=self.id, status="completed", frame_count=self._frame,
            file_path=None, first_sample_at_ns=0, last_sample_at_ns=0,
            health_events=[], error=None,
        )

    def pause_samples(self):
        self._pause.set()

    def resume_samples(self):
        self._pause.clear()

    def _run(self):
        while not self._stop_thread.is_set():
            if not self._pause.is_set():
                self._frame += 1
                self._emit_sample(SampleEvent(
                    stream_id=self.id, frame_number=self._frame,
                    capture_ns=time.monotonic_ns(),
                ))
            time.sleep(self._interval)


@pytest.mark.slow
def test_stall_incident_open_and_close(tmp_path: Path):
    sess = SessionOrchestrator(host_id="test", output_dir=tmp_path)
    stream = FakeStream("cam", target_hz=30.0)
    sess.add(stream)

    sess.connect()
    sess.start(countdown_s=0)

    # Induce stall for 3s — StreamStallDetector default threshold is 2s.
    stream.pause_samples()
    time.sleep(3.0)

    opens = [i for i in sess.health.open_incidents() if i.fingerprint == "cam:stream-stall"]
    assert opens, "stall incident did not open"

    # Recover.
    stream.resume_samples()
    time.sleep(2.5)

    sess.stop()
    sess.disconnect()

    resolved = [i for i in sess.health.resolved_incidents() if i.fingerprint == "cam:stream-stall"]
    assert resolved, "stall incident did not resolve"


@pytest.mark.slow
def test_incidents_jsonl_written(tmp_path: Path):
    sess = SessionOrchestrator(host_id="test", output_dir=tmp_path)
    stream = FakeStream("cam", target_hz=30.0)
    sess.add(stream)

    sess.connect()
    sess.start(countdown_s=0)
    stream.pause_samples()
    time.sleep(2.5)
    sess.stop()
    sess.disconnect()

    # Locate incidents.jsonl — the orchestrator places it inside a session-specific subdir.
    out_files = list(tmp_path.rglob("incidents.jsonl"))
    assert out_files, "no incidents.jsonl written"
    lines = out_files[0].read_text().strip().splitlines()
    fingerprints = [json.loads(l)["fingerprint"] for l in lines]
    assert any(fp == "cam:stream-stall" for fp in fingerprints), \
        f"stall fingerprint missing — found: {set(fingerprints)}"


@pytest.mark.slow
def test_incidents_jsonl_written_with_poller_wired(tmp_path: Path):
    """Regression: SessionPoller must not clobber SessionOrchestrator's persist listener."""
    from syncfield.viewer.poller import SessionPoller

    sess = SessionOrchestrator(host_id="test", output_dir=tmp_path)
    stream = FakeStream("cam", target_hz=30.0)
    sess.add(stream)

    # Spin up a poller as the viewer would.
    poller = SessionPoller(sess)

    sess.connect()
    sess.start(countdown_s=0)
    stream.pause_samples()
    time.sleep(2.5)
    sess.stop()
    sess.disconnect()

    out = list(tmp_path.rglob("incidents.jsonl"))
    assert out, "no incidents.jsonl written — poller likely clobbered persist listener"
    lines = out[0].read_text().strip().splitlines()
    assert any('"fingerprint": "cam:stream-stall"' in l or '"fingerprint":"cam:stream-stall"' in l for l in lines)


@pytest.mark.slow
def test_orchestrator_feeds_writer_stats_to_backpressure_detector(tmp_path: Path):
    from syncfield.health.detector import DetectorBase
    from syncfield.health.severity import Severity

    class WriterStatsSpy(DetectorBase):
        name = "writer-stats-spy"
        default_severity = Severity.INFO

        def __init__(self):
            self.calls = 0

        def observe_writer_stats(self, stream_id, stats):
            self.calls += 1

    sess = SessionOrchestrator(host_id="test", output_dir=tmp_path)
    spy = WriterStatsSpy()
    sess.health.register(spy)
    stream = FakeStream("cam", target_hz=30.0)
    sess.add(stream)

    sess.connect()
    sess.start(countdown_s=0)
    time.sleep(0.6)
    sess.stop()
    sess.disconnect()

    # At 10 Hz throttle + 600ms → ~5-6 calls expected.
    assert spy.calls >= 2, f"writer stats not emitted (got {spy.calls})"
