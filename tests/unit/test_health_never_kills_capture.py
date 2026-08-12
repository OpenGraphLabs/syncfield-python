"""A health event must never be able to kill the capture thread.

Field incident (ogpi-006, 2026-08-12): both 30-minute recordings died at
exactly the segment-rotation boundary. `rotate_recording_segment` closes the
session log writer and only assigns the replacement several statements later,
so for that window `self._log_writer` is non-None but closed — and
`_on_stream_health`'s `is not None` guard let a write through, which raised
`RuntimeError: SessionLogWriter is not open` straight up through
`_emit_health` into the OGLO reader thread and killed it. Rotation perturbs
timing enough to generate the very sequence-gap health events that then land
in that window, so rotation reliably shot itself.

Logging is an observability side effect. It must never take capture down.
"""

from __future__ import annotations

from typing import List

import pytest

from syncfield.orchestrator import SessionOrchestrator
from syncfield.testing import FakeStream
from syncfield.tone import SyncToneConfig
from syncfield.types import HealthEvent, HealthEventKind


class _ClosedLogWriter:
    """Stands in for a SessionLogWriter caught mid-rotation."""

    def __init__(self) -> None:
        self.attempts = 0

    def log_health(self, event) -> None:
        self.attempts += 1
        raise RuntimeError("SessionLogWriter is not open")


def _session(tmp_path) -> SessionOrchestrator:
    return SessionOrchestrator(
        host_id="rig_01",
        output_dir=tmp_path,
        sync_tone=SyncToneConfig.silent(),
    )


def test_closed_log_writer_does_not_propagate_into_the_caller(tmp_path):
    """The exact rotation-window failure: the write raises, the caller lives."""
    session = _session(tmp_path)
    writer = _ClosedLogWriter()
    session._log_writer = writer  # type: ignore[assignment]

    event = HealthEvent(
        stream_id="tactile_right",
        kind=HealthEventKind.DROP,
        at_ns=1,
        detail="dropped ~3 tactile samples (seq gap)",
        fingerprint="tactile_right:drop",
    )
    session._on_stream_health(event)  # must not raise

    assert writer.attempts == 1, "the write is still attempted, just not fatal"


def test_health_tracking_survives_a_failed_log_write(tmp_path):
    """Losing the log line must not lose the event: the incident tracker still
    sees it, so the FinalizationReport and incidents.jsonl stay truthful."""
    session = _session(tmp_path)
    session._log_writer = _ClosedLogWriter()  # type: ignore[assignment]
    observed: List[tuple] = []
    session.health.observe_health = lambda sid, ev: observed.append((sid, ev))  # type: ignore[method-assign]

    event = HealthEvent(
        stream_id="tactile_right",
        kind=HealthEventKind.DROP,
        at_ns=1,
        fingerprint="tactile_right:drop",
    )
    session._on_stream_health(event)

    assert [sid for sid, _ in observed] == ["tactile_right"]


def test_emit_health_isolates_a_raising_callback():
    """Defense in depth at the platform choke point: one bad consumer must not
    take the capture thread with it. `_emit_substream_sample` already isolates
    its callbacks; health had no such guard."""
    stream = FakeStream("tactile_right")
    delivered: List[HealthEvent] = []

    def explode(_event) -> None:
        raise RuntimeError("consumer blew up")

    stream.on_health(explode)
    stream.on_health(delivered.append)

    stream._emit_health(
        HealthEvent(stream_id=stream.id, kind=HealthEventKind.DROP, at_ns=1)
    )  # must not raise

    assert len(delivered) == 1, "a failing callback must not starve the next one"
    assert len(stream._collected_health) == 1, "the report still carries the event"


def test_rotation_swaps_the_log_writer_before_closing_the_sealed_one(tmp_path, monkeypatch):
    """Close the window itself, don't just tolerate it.

    Reader threads read `self._log_writer` without the rotation lock, so the
    sealed writer must already be unreachable by the time it is closed. This
    pins the ordering: at `close()` time the published writer is a different,
    open one — so a concurrent health event lands in the new segment's log
    instead of hitting a closed handle.
    """
    from syncfield import orchestrator as orch

    from tests.unit.test_orchestrator import _RotatableFakeStream

    session = _session(tmp_path)
    stream = _RotatableFakeStream("tactile_right")
    session.add(stream)
    session.start(countdown_s=0)

    published_at_close: List[bool] = []
    real_close = orch.SessionLogWriter.close

    def spy_close(self):
        current = session._log_writer
        published_at_close.append(
            current is not self and getattr(current, "_handle", None) is not None
        )
        return real_close(self)

    monkeypatch.setattr(orch.SessionLogWriter, "close", spy_close)

    session.rotate_recording_segment()
    closes_during_rotation = list(published_at_close)
    session.stop()

    assert closes_during_rotation, "rotation must retire the sealed log writer"
    assert all(closes_during_rotation), (
        "a writer was closed while still published on the orchestrator"
    )
