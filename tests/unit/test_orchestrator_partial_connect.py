"""Partial-connect semantics for SessionOrchestrator.

Relies on the FakeStream helper in syncfield.testing, which supports
`fail_on_start=True` to raise from its connect() path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from syncfield.orchestrator import SessionOrchestrator
from syncfield.testing import FakeStream
from syncfield.types import SessionState


def test_one_stream_fails_others_still_connected(tmp_path: Path):
    sess = SessionOrchestrator(host_id="h", output_dir=tmp_path)
    sess.add(FakeStream("good_a"))
    sess.add(FakeStream("bad", fail_on_start=True))
    sess.add(FakeStream("good_b"))

    sess.connect()

    assert sess.state is SessionState.CONNECTED
    assert sess._stream_states["good_a"] == "connected"
    assert sess._stream_states["bad"] == "failed"
    assert sess._stream_states["good_b"] == "connected"
    assert "bad" in sess._stream_errors
    assert sess._stream_errors["bad"]


def test_all_streams_failing_raises_and_returns_to_idle(tmp_path: Path):
    sess = SessionOrchestrator(host_id="h", output_dir=tmp_path)
    sess.add(FakeStream("a", fail_on_start=True))
    sess.add(FakeStream("b", fail_on_start=True))

    with pytest.raises(RuntimeError, match="no streams"):
        sess.connect()

    assert sess.state is SessionState.IDLE
    assert sess._stream_states["a"] == "failed"
    assert sess._stream_states["b"] == "failed"


def test_startup_failure_event_reaches_health_system(tmp_path: Path):
    from syncfield.health.detector import DetectorBase
    from syncfield.health.severity import Severity

    class Spy(DetectorBase):
        name = "startup-spy"
        default_severity = Severity.INFO

        def __init__(self):
            self.events = []

        def observe_health(self, stream_id, event):
            self.events.append(event)

    sess = SessionOrchestrator(host_id="h", output_dir=tmp_path)
    spy = Spy()
    sess.health.register(spy)
    sess.add(FakeStream("good"))
    sess.add(FakeStream("bad", fail_on_start=True))

    sess.connect()

    import time
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not any(
        e.fingerprint == "bad:startup-failure" for e in spy.events
    ):
        time.sleep(0.02)

    failure_events = [e for e in spy.events if e.fingerprint == "bad:startup-failure"]
    assert failure_events, "no startup-failure event observed"
    ev = failure_events[0]
    assert ev.data.get("phase") == "connect"
    assert ev.data.get("outcome") == "error"
    assert ev.data.get("error")
