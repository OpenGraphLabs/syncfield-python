"""End-to-end: a stream that connects but never emits a sample triggers
the no-data incident within the configured threshold."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from syncfield.orchestrator import SessionOrchestrator
from syncfield.stream import StreamBase
from syncfield.types import FinalizationReport, SampleEvent, StreamCapabilities


class SilentFakeStream(StreamBase):
    """FakeStream variant that connects successfully but emits no samples until asked."""

    def __init__(self, stream_id: str):
        super().__init__(id=stream_id, kind="sensor", capabilities=StreamCapabilities())
        self._stop = threading.Event()
        self._gate = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame = 0

    def connect(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def disconnect(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def start_recording(self, session_clock):
        pass

    def stop_recording(self) -> FinalizationReport:
        return FinalizationReport(
            stream_id=self.id, status="completed", frame_count=self._frame,
            file_path=None, first_sample_at_ns=0, last_sample_at_ns=0,
            health_events=[], error=None,
        )

    def allow_samples(self):
        self._gate.set()

    def _run(self):
        while not self._stop.is_set():
            if self._gate.is_set():
                self._frame += 1
                self._emit_sample(SampleEvent(
                    stream_id=self.id, frame_number=self._frame,
                    capture_ns=time.monotonic_ns(),
                ))
            time.sleep(0.05)


@pytest.mark.slow
def test_no_data_incident_opens_then_closes_when_samples_arrive(tmp_path: Path):
    sess = SessionOrchestrator(host_id="h", output_dir=tmp_path)
    stream = SilentFakeStream("cam")
    sess.add(stream)

    for d in sess.health.iter_detectors():
        if d.name == "no-data":
            d._threshold_ns = int(1e9)
            break

    sess.connect()

    time.sleep(1.5)
    open_fps = [i.fingerprint for i in sess.health.open_incidents()]
    assert "cam:no-data" in open_fps

    stream.allow_samples()
    time.sleep(0.5)

    # Session was never started into RECORDING — disconnect directly from CONNECTED.
    sess.disconnect()

    resolved_fps = [i.fingerprint for i in sess.health.resolved_incidents()]
    assert "cam:no-data" in resolved_fps
