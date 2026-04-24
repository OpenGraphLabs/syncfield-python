"""Orchestrator fans out a single armed_host_ns to all streams."""
from __future__ import annotations

import json
import time
from pathlib import Path

from syncfield.clock import SessionClock
from syncfield.orchestrator import SessionOrchestrator
from syncfield.stream import StreamBase
from syncfield.tone import SyncToneConfig
from syncfield.types import (
    FinalizationReport,
    RecordingAnchor,
    StreamCapabilities,
)


class _CaptureClockStream(StreamBase):
    """Records the SessionClock passed to start_recording()."""

    def __init__(self, id: str) -> None:
        super().__init__(id, "sensor", StreamCapabilities())
        self.received_clock: SessionClock | None = None

    def connect(self) -> None:
        pass

    def start_recording(self, session_clock: SessionClock) -> None:
        self.received_clock = session_clock

    def stop_recording(self) -> FinalizationReport:
        anchor = None
        if self.received_clock and self.received_clock.recording_armed_ns:
            anchor = RecordingAnchor(
                armed_host_ns=self.received_clock.recording_armed_ns,
                first_frame_host_ns=self.received_clock.recording_armed_ns + 1_000,
                first_frame_device_ns=None,
            )
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=1,
            file_path=None,
            first_sample_at_ns=0,
            last_sample_at_ns=0,
            health_events=[],
            error=None,
            recording_anchor=anchor,
        )

    def disconnect(self) -> None:
        pass


def _mk_session(tmp_path: Path) -> SessionOrchestrator:
    """Build a silent-chirp orchestrator ready for tests."""
    return SessionOrchestrator(
        host_id="h",
        output_dir=tmp_path,
        sync_tone=SyncToneConfig.silent(),
    )


def test_orchestrator_arms_clock_and_all_streams_see_same_armed_ns(
    tmp_path: Path,
) -> None:
    sess = _mk_session(tmp_path)
    a = _CaptureClockStream("a")
    b = _CaptureClockStream("b")
    sess.add(a)
    sess.add(b)
    sess.connect()
    sess.start(countdown_s=0)
    time.sleep(0.01)
    sess.stop()

    assert a.received_clock is not None and b.received_clock is not None
    assert a.received_clock.recording_armed_ns is not None
    assert (
        a.received_clock.recording_armed_ns
        == b.received_clock.recording_armed_ns
    )


def test_orchestrator_manifest_includes_per_stream_anchor(tmp_path: Path) -> None:
    sess = _mk_session(tmp_path)
    a = _CaptureClockStream("a")
    sess.add(a)
    sess.connect()
    sess.start(countdown_s=0)
    time.sleep(0.01)
    sess.stop()

    manifest_paths = list(tmp_path.rglob("manifest.json"))
    assert manifest_paths, "manifest.json not written"
    manifest = json.loads(manifest_paths[0].read_text())
    streams = manifest.get("streams", {})
    # The orchestrator writes streams as a dict keyed by stream_id.
    assert "a" in streams, f"stream 'a' not in manifest streams: {streams!r}"
    a_entry = streams["a"]
    assert "recording_anchor" in a_entry
    assert a_entry["recording_anchor"] is not None
    assert "armed_host_ns" in a_entry["recording_anchor"]
    assert "first_frame_host_ns" in a_entry["recording_anchor"]
    assert "first_frame_latency_ns" in a_entry["recording_anchor"]
