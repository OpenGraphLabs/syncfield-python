"""Platform-fill contract for HealthEvent — StreamBase._emit_health() must
fill in ``fingerprint`` / ``severity`` for adapter events that left them at
their safe defaults, per the contract documented on
:class:`~syncfield.types.HealthEvent`.

Regression coverage for the bug where no adapter ever set ``fingerprint``,
so every adapter-emitted HealthEvent was silently dropped by
``IncidentTracker.ingest()`` (caught and logged as a warning by
``HealthWorker._safe_ingest``) and never became an Incident.
"""

from __future__ import annotations

import json
from typing import List

from syncfield.health.severity import Severity
from syncfield.health.tracker import IncidentTracker
from syncfield.orchestrator import SessionOrchestrator
from syncfield.testing import FakeStream
from syncfield.tone import SyncToneConfig
from syncfield.types import HealthEvent, HealthEventKind


def _session(tmp_path) -> SessionOrchestrator:
    """Minimal silent-chirp, zero-countdown session for fast unit tests."""
    session = SessionOrchestrator(
        host_id="rig_01",
        output_dir=tmp_path,
        sync_tone=SyncToneConfig.silent(),
    )
    _real_start = session.start

    def _fast_start(*args, **kwargs):
        kwargs.setdefault("countdown_s", 0)
        return _real_start(*args, **kwargs)

    session.start = _fast_start  # type: ignore[method-assign]
    return session


def test_bare_health_event_gets_fingerprint_and_severity_filled():
    """A bare adapter event (safe defaults left untouched) gets both fields
    filled by StreamBase._emit_health before reaching any callback.
    """
    fs = FakeStream("cam")
    received: List[HealthEvent] = []
    fs.on_health(received.append)

    fs._emit_health(HealthEvent(stream_id=fs.id, kind=HealthEventKind.DROP, at_ns=1))

    assert len(received) == 1
    assert received[0].fingerprint == "cam:drop"
    assert received[0].severity == Severity.WARNING


def test_explicit_fingerprint_is_preserved():
    """An adapter that already set fingerprint keeps its own value."""
    fs = FakeStream("cam")
    received: List[HealthEvent] = []
    fs.on_health(received.append)

    fs._emit_health(
        HealthEvent(
            stream_id=fs.id,
            kind=HealthEventKind.DROP,
            at_ns=1,
            fingerprint="cam:custom-fingerprint",
        )
    )

    assert received[0].fingerprint == "cam:custom-fingerprint"


def test_explicit_severity_is_preserved():
    """An adapter that explicitly set severity=ERROR on a DROP keeps ERROR,
    not the WARNING the platform would otherwise infer from the kind.
    """
    fs = FakeStream("cam")
    received: List[HealthEvent] = []
    fs.on_health(received.append)

    fs._emit_health(
        HealthEvent(
            stream_id=fs.id,
            kind=HealthEventKind.DROP,
            at_ns=1,
            severity=Severity.ERROR,
        )
    )

    assert received[0].severity == Severity.ERROR
    # fingerprint was left at its default, so the platform still fills it.
    assert received[0].fingerprint == "cam:drop"


def test_tracker_accepts_platform_filled_event():
    """IncidentTracker.ingest() must accept a platform-filled event without
    raising — previously every bare adapter event raised ValueError here
    (silently swallowed by HealthWorker._safe_ingest).
    """
    fs = FakeStream("cam")
    captured: List[HealthEvent] = []
    fs.on_health(captured.append)

    fs._emit_health(HealthEvent(stream_id=fs.id, kind=HealthEventKind.DROP, at_ns=1))

    tracker = IncidentTracker()
    tracker.ingest(captured[0])  # must not raise

    assert len(tracker.open_incidents()) == 1
    assert tracker.open_incidents()[0].fingerprint == "cam:drop"


def test_two_drop_events_group_into_one_incident_with_occurrence_count_2(tmp_path):
    """End-to-end: a real SessionOrchestrator + FakeStream emitting two bare
    DROP events during recording must land as ONE incident in
    incidents.jsonl with event_count == 2 (Sentry-style grouping by the
    platform-filled fingerprint).
    """
    session = _session(tmp_path)
    fs = FakeStream("cam")
    session.add(fs)
    session.start()

    fs._emit_health(HealthEvent(stream_id=fs.id, kind=HealthEventKind.DROP, at_ns=1_000_000))
    fs._emit_health(HealthEvent(stream_id=fs.id, kind=HealthEventKind.DROP, at_ns=2_000_000))

    session.stop()

    incidents_path = session.last_episode_dir / "incidents.jsonl"
    assert incidents_path.exists(), "incidents.jsonl was not written"
    lines = [json.loads(line) for line in incidents_path.read_text().strip().splitlines()]

    cam_lines = [line for line in lines if line["fingerprint"] == "cam:drop"]
    assert cam_lines, f"no cam:drop incident persisted; got fingerprints: {[l['fingerprint'] for l in lines]}"

    ids = {line["id"] for line in cam_lines}
    assert len(ids) == 1, f"expected exactly one incident for cam:drop, got {ids}"

    # The last persisted line for this incident reflects its final state.
    assert cam_lines[-1]["event_count"] == 2
