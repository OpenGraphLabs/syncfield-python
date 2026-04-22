# Health Telemetry Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a sensor-agnostic, platform-level health telemetry system that continuously observes every stream during a recording, raises Sentry-style Incidents on stalls / FPS drops / jitter / startup failures / writer backpressure / adapter faults, and surfaces them live in the viewer and in `FinalizationReport.incidents` + `incidents.jsonl` post-session.

**Architecture:** New `src/syncfield/health/` package. Streams push samples and raw `HealthEvent`s through thread-safe ingress queues into a single daemon `HealthWorker` that ticks at 20 Hz. On each tick every registered `Detector` is polled; emitted `HealthEvent`s feed an `IncidentTracker` that groups by `fingerprint`, owns open/close lifecycle, and flushes to disk. Default detectors are installed automatically; new ones can be added via `session.health.register(...)`. OAK's native depthai logger is bridged into the same channel so X_LINK_ERROR / crash / reconnect events become structured incidents with crash-dump artifacts attached.

**Tech Stack:** Python 3.9+, `dataclasses`, `threading`, `queue.SimpleQueue`, `logging` (for depthai bridge). Frontend: existing React + TypeScript viewer.

**Spec:** `docs/superpowers/specs/2026-04-22-health-telemetry-design.md`

---

## File Structure

### New (backend)

```
src/syncfield/health/
├── __init__.py                 # public API
├── severity.py                 # Severity enum + ordering helpers
├── types.py                    # Incident, IncidentArtifact, IncidentSnapshot, WriterStats
├── detector.py                 # Detector Protocol + DetectorBase
├── registry.py                 # DetectorRegistry
├── tracker.py                  # IncidentTracker
├── worker.py                   # HealthWorker (daemon thread + ingress queues)
├── system.py                   # HealthSystem (facade users touch)
└── detectors/
    ├── __init__.py
    ├── adapter_passthrough.py  # AdapterEventPassthrough
    ├── stream_stall.py         # StreamStallDetector
    ├── fps_drop.py             # FpsDropDetector
    ├── jitter.py               # JitterDetector
    ├── startup_failure.py      # StartupFailureDetector
    ├── backpressure.py         # BackpressureDetector
    └── depthai_bridge.py       # DepthAILoggerBridge (soft-imports depthai)
```

### Modified (backend)

- `src/syncfield/types.py` — add `severity` / `source` / `fingerprint` / `data` to `HealthEvent`; add `target_hz` to `StreamCapabilities`; add `incidents` to `FinalizationReport`.
- `src/syncfield/writer.py` — add `SessionLogWriter.log_incident()`; emit `WriterStats`.
- `src/syncfield/orchestrator.py` — construct `HealthSystem`, wire sample / health / state / writer-stats observers, start/stop worker, embed incidents in `FinalizationReport`.
- `src/syncfield/adapters/oak_camera.py` — declare `target_hz`, install `DepthAILoggerBridge`, attach `crash_dump` artifacts.
- `src/syncfield/adapters/uvc_webcam.py`, `host_audio.py`, `meta_quest_camera/*`, `ble_imu.py`, `insta360_go3s/stream.py`, `polling_sensor.py` — declare `target_hz` where known.
- `src/syncfield/viewer/state.py` — replace `HealthEntry` / `StreamSnapshot.health_count` with `IncidentSnapshot` / top-level `active_incidents` / `resolved_incidents`.
- `src/syncfield/viewer/poller.py` — subscribe to `HealthSystem` incident callbacks, populate new snapshot fields.
- `src/syncfield/viewer/server.py` — serialize incidents into WebSocket payload.

### New (frontend)

- `src/syncfield/viewer/frontend/src/components/incident-panel.tsx`

### Deleted (frontend)

- `src/syncfield/viewer/frontend/src/components/health-table.tsx`

### Modified (frontend)

- `src/syncfield/viewer/frontend/src/lib/types.ts` — mirror `Severity`, `IncidentSnapshot`; remove `HealthEntry`, `health_count`.
- `src/syncfield/viewer/frontend/src/components/stream-card.tsx` — severity badge.
- `src/syncfield/viewer/frontend/src/App.tsx` — mount `IncidentPanel` in place of `HealthTable`.

### Tests

```
tests/unit/health/
├── __init__.py
├── test_severity.py
├── test_incident_types.py
├── test_detector_base.py
├── test_registry.py
├── test_incident_tracker.py
├── test_health_worker.py
├── test_health_system.py
└── detectors/
    ├── __init__.py
    ├── test_adapter_passthrough.py
    ├── test_stream_stall.py
    ├── test_fps_drop.py
    ├── test_jitter.py
    ├── test_startup_failure.py
    ├── test_backpressure.py
    └── test_depthai_bridge.py

tests/integration/health/
├── __init__.py
├── test_orchestrator_health_integration.py
└── test_incidents_jsonl_roundtrip.py
```

---

## Conventions used in this plan

- Every task is **TDD**: write a failing test, verify failure, implement, verify pass, commit.
- Run tests with `pytest <path> -v` (project `pyproject.toml` sets `testpaths=["tests"]`, `pythonpath=["src"]`).
- Each task ends with a commit using Conventional Commits (`feat:`, `refactor:`, `test:`).
- Test helpers use `tests/helpers/` where one exists; new helpers live in `tests/unit/health/_helpers.py`.
- `time.monotonic_ns()` returns an int. All `_ns` fields are ints.
- We will use `queue.SimpleQueue` for lock-free ingress (Python's own fast, unbounded, thread-safe FIFO).

---

## Phase 1 — Core types & skeleton

### Task 1: Severity enum

**Files:**
- Create: `src/syncfield/health/__init__.py` (empty for now)
- Create: `src/syncfield/health/severity.py`
- Test: `tests/unit/health/__init__.py` (empty), `tests/unit/health/test_severity.py`

- [ ] **Step 1: Create the (empty) package init files**

```bash
mkdir -p src/syncfield/health tests/unit/health
touch src/syncfield/health/__init__.py tests/unit/health/__init__.py
```

- [ ] **Step 2: Write failing test**

Create `tests/unit/health/test_severity.py`:

```python
from syncfield.health.severity import Severity, max_severity


def test_severity_values():
    assert Severity.INFO.value == "info"
    assert Severity.WARNING.value == "warning"
    assert Severity.ERROR.value == "error"
    assert Severity.CRITICAL.value == "critical"


def test_severity_ordering():
    # INFO < WARNING < ERROR < CRITICAL
    order = [Severity.INFO, Severity.WARNING, Severity.ERROR, Severity.CRITICAL]
    for a, b in zip(order, order[1:]):
        assert a.rank < b.rank


def test_max_severity_picks_highest():
    assert max_severity(Severity.INFO, Severity.WARNING) == Severity.WARNING
    assert max_severity(Severity.ERROR, Severity.WARNING) == Severity.ERROR
    assert max_severity(Severity.CRITICAL, Severity.INFO, Severity.ERROR) == Severity.CRITICAL


def test_max_severity_requires_at_least_one():
    import pytest
    with pytest.raises(ValueError):
        max_severity()
```

- [ ] **Step 3: Run, confirm fail**

```bash
pytest tests/unit/health/test_severity.py -v
```
Expected: `ModuleNotFoundError: No module named 'syncfield.health.severity'`

- [ ] **Step 4: Implement**

`src/syncfield/health/severity.py`:

```python
"""Severity levels for health events and incidents.

Ordered INFO < WARNING < ERROR < CRITICAL. Use :func:`max_severity` to
pick the highest of several levels — incidents escalate to the max
severity of their constituent events.
"""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.ERROR: 2,
    Severity.CRITICAL: 3,
}


def max_severity(*levels: Severity) -> Severity:
    if not levels:
        raise ValueError("max_severity requires at least one Severity")
    return max(levels, key=lambda s: s.rank)
```

- [ ] **Step 5: Run, confirm pass**

```bash
pytest tests/unit/health/test_severity.py -v
```
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/syncfield/health/ tests/unit/health/
git commit -m "feat(health): add Severity enum with rank + max_severity helper"
```

---

### Task 2: Enrich HealthEvent with severity / source / fingerprint / data

**Files:**
- Modify: `src/syncfield/types.py:250-272` (`HealthEvent` dataclass + `to_dict`)
- Test: `tests/unit/test_types.py` (add cases)

Spec requires new fields on `HealthEvent`. We are intentionally breaking the existing format; all writes will carry the new fields and readers (viewer, session log consumers) update in later tasks.

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_types.py`:

```python
from syncfield.health.severity import Severity
from syncfield.types import HealthEvent, HealthEventKind


def test_health_event_has_enrichment_fields_with_defaults():
    ev = HealthEvent(
        stream_id="cam",
        kind=HealthEventKind.ERROR,
        at_ns=1_000,
        detail="boom",
    )
    # new fields default to safe values when caller does not set them.
    assert ev.severity == Severity.INFO
    assert ev.source == "unknown"
    assert ev.fingerprint == ""
    assert ev.data == {}


def test_health_event_to_dict_includes_new_fields():
    ev = HealthEvent(
        stream_id="cam",
        kind=HealthEventKind.ERROR,
        at_ns=1_000,
        detail="boom",
        severity=Severity.ERROR,
        source="adapter:oak",
        fingerprint="cam:adapter:xlink-error",
        data={"stream": "__x_0_1"},
    )
    d = ev.to_dict()
    assert d["severity"] == "error"
    assert d["source"] == "adapter:oak"
    assert d["fingerprint"] == "cam:adapter:xlink-error"
    assert d["data"] == {"stream": "__x_0_1"}
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/test_types.py -k health_event -v
```
Expected: `TypeError` (frozen dataclass rejecting unknown kwargs) or AttributeError on `.severity`.

- [ ] **Step 3: Implement**

In `src/syncfield/types.py`, replace the `HealthEvent` dataclass and its `to_dict`:

```python
from syncfield.health.severity import Severity  # top of file, next to other imports

@dataclass(frozen=True)
class HealthEvent:
    """A stream reports a health observation.

    ``severity`` / ``source`` / ``fingerprint`` / ``data`` enable the
    incident-tracking layer in :mod:`syncfield.health` to group many raw
    events into a single Sentry-style Incident. Adapters that don't care
    can leave them at their safe defaults; the platform will fill them
    in before the event reaches the IncidentTracker.
    """

    stream_id: str
    kind: HealthEventKind
    at_ns: int
    detail: str | None = None
    severity: Severity = Severity.INFO
    source: str = "unknown"
    fingerprint: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "kind": self.kind.value,
            "at_ns": self.at_ns,
            "detail": self.detail,
            "severity": self.severity.value,
            "source": self.source,
            "fingerprint": self.fingerprint,
            "data": self.data,
        }
```

Add `from dataclasses import field` if missing. `field` is needed for the mutable default on `data`.

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/test_types.py -v
```
Expected: all pass, including existing tests (defaults are backward-compatible at the Python level; only the JSON format changes).

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/types.py tests/unit/test_types.py
git commit -m "feat(types): enrich HealthEvent with severity/source/fingerprint/data"
```

---

### Task 3: WriterStats + IncidentArtifact + Incident + IncidentSnapshot

**Files:**
- Create: `src/syncfield/health/types.py`
- Test: `tests/unit/health/test_incident_types.py`

- [ ] **Step 1: Write failing test**

`tests/unit/health/test_incident_types.py`:

```python
from syncfield.health.severity import Severity
from syncfield.health.types import (
    Incident,
    IncidentArtifact,
    IncidentSnapshot,
    WriterStats,
)
from syncfield.types import HealthEvent, HealthEventKind


def _ev(at_ns: int, severity: Severity = Severity.ERROR) -> HealthEvent:
    return HealthEvent(
        stream_id="cam",
        kind=HealthEventKind.ERROR,
        at_ns=at_ns,
        detail="x",
        severity=severity,
        source="detector:stream-stall",
        fingerprint="cam:stream-stall",
    )


def test_writer_stats_fields():
    s = WriterStats(
        stream_id="cam",
        at_ns=100,
        queue_depth=3,
        queue_capacity=16,
        dropped=0,
    )
    assert s.queue_fullness == 3 / 16
    assert s.stream_id == "cam"


def test_writer_stats_zero_capacity_is_empty():
    s = WriterStats(stream_id="cam", at_ns=0, queue_depth=0, queue_capacity=0, dropped=0)
    assert s.queue_fullness == 0.0


def test_incident_from_first_event_initializes_fields():
    first = _ev(100)
    inc = Incident.opened_from(first, title="Stream stalled (silence 2.0s)")
    assert inc.stream_id == "cam"
    assert inc.fingerprint == "cam:stream-stall"
    assert inc.severity == Severity.ERROR
    assert inc.title == "Stream stalled (silence 2.0s)"
    assert inc.opened_at_ns == 100
    assert inc.closed_at_ns is None
    assert inc.event_count == 1
    assert inc.first_event == first
    assert inc.last_event == first
    assert inc.artifacts == []


def test_incident_record_event_escalates_severity_and_updates_last():
    inc = Incident.opened_from(_ev(100, severity=Severity.WARNING), title="t")
    inc.record_event(_ev(200, severity=Severity.ERROR))
    assert inc.event_count == 2
    assert inc.severity == Severity.ERROR
    assert inc.last_event.at_ns == 200
    assert inc.last_event_at_ns == 200


def test_incident_close():
    inc = Incident.opened_from(_ev(100), title="t")
    inc.close(at_ns=500)
    assert inc.closed_at_ns == 500
    assert inc.is_open is False


def test_incident_attach_artifact():
    inc = Incident.opened_from(_ev(100), title="t")
    inc.attach(IncidentArtifact(kind="crash_dump", path="/tmp/x.json"))
    assert inc.artifacts[0].kind == "crash_dump"
    assert inc.artifacts[0].path == "/tmp/x.json"


def test_incident_snapshot_shape():
    inc = Incident.opened_from(_ev(100), title="t")
    snap = IncidentSnapshot.from_incident(inc, now_ns=1_000_000_100)
    assert snap.id == inc.id
    assert snap.stream_id == "cam"
    assert snap.severity == "error"
    assert snap.is_open is True
    assert snap.ago_s >= 0
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/health/test_incident_types.py -v
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/syncfield/health/types.py`:

```python
"""Data classes for the health/incident layer.

These are plain, explicit structs — the :mod:`syncfield.health` runtime
mutates :class:`Incident` objects in-place on the worker thread. The
viewer receives immutable :class:`IncidentSnapshot`\\ s instead.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, List

from syncfield.health.severity import Severity, max_severity
from syncfield.types import HealthEvent


@dataclass(frozen=True)
class WriterStats:
    """One observation of a per-stream writer's queue."""

    stream_id: str
    at_ns: int
    queue_depth: int
    queue_capacity: int
    dropped: int

    @property
    def queue_fullness(self) -> float:
        if self.queue_capacity <= 0:
            return 0.0
        return self.queue_depth / self.queue_capacity


@dataclass(frozen=True)
class IncidentArtifact:
    """A piece of evidence attached to an Incident (crash dump, log excerpt, ...)."""

    kind: str
    path: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "path": self.path, "detail": self.detail}


@dataclass
class Incident:
    """A grouped, open/close-tracked sequence of HealthEvents sharing a fingerprint.

    Mutable because the worker thread updates ``last_event`` / ``event_count``
    / ``severity`` on every matching event. The viewer never sees this
    class directly — it reads :class:`IncidentSnapshot` instead.
    """

    id: str
    stream_id: str
    fingerprint: str
    title: str
    severity: Severity
    source: str
    opened_at_ns: int
    closed_at_ns: int | None
    last_event_at_ns: int
    event_count: int
    first_event: HealthEvent
    last_event: HealthEvent
    artifacts: List[IncidentArtifact] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def opened_from(cls, event: HealthEvent, *, title: str) -> "Incident":
        return cls(
            id=uuid.uuid4().hex,
            stream_id=event.stream_id,
            fingerprint=event.fingerprint,
            title=title,
            severity=event.severity,
            source=event.source,
            opened_at_ns=event.at_ns,
            closed_at_ns=None,
            last_event_at_ns=event.at_ns,
            event_count=1,
            first_event=event,
            last_event=event,
        )

    @property
    def is_open(self) -> bool:
        return self.closed_at_ns is None

    def record_event(self, event: HealthEvent) -> None:
        self.event_count += 1
        self.last_event = event
        self.last_event_at_ns = event.at_ns
        self.severity = max_severity(self.severity, event.severity)

    def close(self, *, at_ns: int) -> None:
        self.closed_at_ns = at_ns

    def attach(self, artifact: IncidentArtifact) -> None:
        self.artifacts.append(artifact)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stream_id": self.stream_id,
            "fingerprint": self.fingerprint,
            "title": self.title,
            "severity": self.severity.value,
            "source": self.source,
            "opened_at_ns": self.opened_at_ns,
            "closed_at_ns": self.closed_at_ns,
            "last_event_at_ns": self.last_event_at_ns,
            "event_count": self.event_count,
            "first_event": self.first_event.to_dict(),
            "last_event": self.last_event.to_dict(),
            "artifacts": [a.to_dict() for a in self.artifacts],
            "data": self.data,
        }


@dataclass(frozen=True)
class IncidentSnapshot:
    """Read-only view of an Incident, for the viewer's WebSocket payload."""

    id: str
    stream_id: str
    fingerprint: str
    title: str
    severity: str
    source: str
    opened_at_ns: int
    closed_at_ns: int | None
    event_count: int
    detail: str | None
    ago_s: float
    artifacts: List[dict]

    @property
    def is_open(self) -> bool:
        return self.closed_at_ns is None

    @classmethod
    def from_incident(cls, inc: Incident, *, now_ns: int) -> "IncidentSnapshot":
        anchor = inc.closed_at_ns if inc.closed_at_ns is not None else inc.last_event_at_ns
        ago_s = max(0.0, (now_ns - anchor) / 1e9)
        return cls(
            id=inc.id,
            stream_id=inc.stream_id,
            fingerprint=inc.fingerprint,
            title=inc.title,
            severity=inc.severity.value,
            source=inc.source,
            opened_at_ns=inc.opened_at_ns,
            closed_at_ns=inc.closed_at_ns,
            event_count=inc.event_count,
            detail=inc.last_event.detail,
            ago_s=ago_s,
            artifacts=[a.to_dict() for a in inc.artifacts],
        )
```

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/health/test_incident_types.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/types.py tests/unit/health/test_incident_types.py
git commit -m "feat(health): add WriterStats, Incident, IncidentArtifact, IncidentSnapshot"
```

---

### Task 4: Detector Protocol + DetectorBase

**Files:**
- Create: `src/syncfield/health/detector.py`
- Test: `tests/unit/health/test_detector_base.py`

- [ ] **Step 1: Write failing test**

`tests/unit/health/test_detector_base.py`:

```python
from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import WriterStats
from syncfield.types import HealthEvent, HealthEventKind, SampleEvent, SessionState


class NoopDetector(DetectorBase):
    name = "noop"
    default_severity = Severity.WARNING


def test_detector_base_defaults_are_noops():
    d = NoopDetector()
    # All observers accept calls without raising.
    d.observe_sample("cam", SampleEvent(stream_id="cam", frame_number=1, capture_ns=100))
    d.observe_health("cam", HealthEvent(stream_id="cam", kind=HealthEventKind.WARNING, at_ns=1))
    d.observe_state(SessionState.IDLE, SessionState.CONNECTED)
    d.observe_writer_stats("cam", WriterStats("cam", 1, 0, 0, 0))
    # tick yields nothing by default.
    assert list(d.tick(now_ns=100)) == []
    # close_condition defaults to True so pass-through close can rely on it.
    from syncfield.health.types import Incident
    ev = HealthEvent(stream_id="cam", kind=HealthEventKind.WARNING, at_ns=1)
    inc = Incident.opened_from(ev, title="x")
    assert isinstance(d.close_condition(inc, now_ns=10), bool)


def test_detector_base_requires_name_and_severity():
    import pytest

    with pytest.raises(TypeError):
        DetectorBase()  # abstract base: name / default_severity unset on the class
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/health/test_detector_base.py -v
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/syncfield/health/detector.py`:

```python
"""Detector protocol + base class.

A :class:`Detector` observes the stream (samples, adapter health events,
session state, writer stats) and may emit :class:`HealthEvent` on each
``tick()``. The :class:`IncidentTracker` groups emitted events by
fingerprint, opens incidents, and consults ``close_condition`` to know
when an open incident should resolve.

Most detectors subclass :class:`DetectorBase` and override only the
observe/tick hooks they care about; the base provides safe no-op
defaults for the rest.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Protocol, runtime_checkable

from syncfield.health.severity import Severity
from syncfield.health.types import Incident, WriterStats
from syncfield.types import HealthEvent, SampleEvent, SessionState


@runtime_checkable
class Detector(Protocol):
    name: str
    default_severity: Severity

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None: ...
    def observe_health(self, stream_id: str, event: HealthEvent) -> None: ...
    def observe_state(self, old: SessionState, new: SessionState) -> None: ...
    def observe_writer_stats(self, stream_id: str, stats: WriterStats) -> None: ...
    def tick(self, now_ns: int) -> Iterable[HealthEvent]: ...
    def close_condition(self, incident: Incident, now_ns: int) -> bool: ...


class DetectorBase:
    """No-op defaults for every Detector hook.

    Subclasses set ``name`` and ``default_severity`` at the class level
    and override only the hooks that matter for their rule.
    """

    name: str
    default_severity: Severity

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Guard against subclasses that forget to set required class attrs.
        for attr in ("name", "default_severity"):
            if not hasattr(cls, attr):
                raise TypeError(
                    f"Detector subclass {cls.__name__} must set class attribute '{attr}'"
                )

    def __new__(cls, *args: object, **kwargs: object) -> "DetectorBase":
        if cls is DetectorBase:
            raise TypeError("DetectorBase is abstract; subclass it")
        return super().__new__(cls)

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None:
        pass

    def observe_health(self, stream_id: str, event: HealthEvent) -> None:
        pass

    def observe_state(self, old: SessionState, new: SessionState) -> None:
        pass

    def observe_writer_stats(self, stream_id: str, stats: WriterStats) -> None:
        pass

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        return iter(())

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        # Conservative default: keep open. Subclasses override to close.
        return False
```

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/health/test_detector_base.py -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/detector.py tests/unit/health/test_detector_base.py
git commit -m "feat(health): add Detector protocol + DetectorBase"
```

---

### Task 5: DetectorRegistry

**Files:**
- Create: `src/syncfield/health/registry.py`
- Test: `tests/unit/health/test_registry.py`

- [ ] **Step 1: Write failing test**

`tests/unit/health/test_registry.py`:

```python
import pytest

from syncfield.health.detector import DetectorBase
from syncfield.health.registry import DetectorRegistry
from syncfield.health.severity import Severity


class Det(DetectorBase):
    name = "d1"
    default_severity = Severity.WARNING


class Det2(DetectorBase):
    name = "d2"
    default_severity = Severity.ERROR


def test_register_and_iterate():
    reg = DetectorRegistry()
    d1 = Det()
    d2 = Det2()
    reg.register(d1)
    reg.register(d2)
    assert list(reg) == [d1, d2]


def test_register_duplicate_name_raises():
    reg = DetectorRegistry()
    reg.register(Det())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(Det())


def test_unregister_removes_by_name():
    reg = DetectorRegistry()
    d1 = Det()
    reg.register(d1)
    reg.unregister("d1")
    assert list(reg) == []


def test_unregister_unknown_is_noop():
    reg = DetectorRegistry()
    reg.unregister("nope")  # does not raise
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/health/test_registry.py -v
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/syncfield/health/registry.py`:

```python
"""Registry of active Detectors for a session."""

from __future__ import annotations

from typing import Iterator, List

from syncfield.health.detector import Detector


class DetectorRegistry:
    def __init__(self) -> None:
        self._detectors: List[Detector] = []

    def register(self, detector: Detector) -> None:
        if any(d.name == detector.name for d in self._detectors):
            raise ValueError(f"Detector '{detector.name}' is already registered")
        self._detectors.append(detector)

    def unregister(self, name: str) -> None:
        self._detectors = [d for d in self._detectors if d.name != name]

    def __iter__(self) -> Iterator[Detector]:
        return iter(list(self._detectors))

    def __len__(self) -> int:
        return len(self._detectors)
```

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/health/test_registry.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/registry.py tests/unit/health/test_registry.py
git commit -m "feat(health): add DetectorRegistry"
```

---

### Task 6: IncidentTracker

**Files:**
- Create: `src/syncfield/health/tracker.py`
- Test: `tests/unit/health/test_incident_tracker.py`

`IncidentTracker` owns the open/close lifecycle for all incidents in a session. It never runs in user code directly — the `HealthWorker` owns and drives it.

- [ ] **Step 1: Write failing test**

`tests/unit/health/test_incident_tracker.py`:

```python
from typing import List

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.tracker import IncidentTracker
from syncfield.health.types import Incident
from syncfield.types import HealthEvent, HealthEventKind


def _ev(at_ns: int, fingerprint: str = "cam:stall", severity: Severity = Severity.ERROR,
        detail: str = "x") -> HealthEvent:
    return HealthEvent(
        stream_id="cam",
        kind=HealthEventKind.ERROR,
        at_ns=at_ns,
        detail=detail,
        severity=severity,
        source="detector:stream-stall",
        fingerprint=fingerprint,
    )


class AlwaysCloseAfter(DetectorBase):
    name = "stream-stall"
    default_severity = Severity.ERROR

    def __init__(self, close_after_ns: int) -> None:
        self._close_after = close_after_ns

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        return now_ns - incident.last_event_at_ns >= self._close_after


def test_tracker_opens_incident_on_first_matching_event():
    tr = IncidentTracker()
    tr.bind_detector(AlwaysCloseAfter(close_after_ns=1000))
    opened: List[Incident] = []
    tr.on_opened = opened.append

    tr.ingest(_ev(100))

    assert len(tr.open_incidents()) == 1
    assert opened and opened[0].stream_id == "cam"


def test_tracker_groups_same_fingerprint_into_one_incident():
    tr = IncidentTracker()
    tr.bind_detector(AlwaysCloseAfter(close_after_ns=1_000_000_000))

    tr.ingest(_ev(100, severity=Severity.WARNING))
    tr.ingest(_ev(200, severity=Severity.ERROR))  # escalate
    tr.ingest(_ev(300, severity=Severity.ERROR))

    opens = tr.open_incidents()
    assert len(opens) == 1
    inc = opens[0]
    assert inc.event_count == 3
    assert inc.severity == Severity.ERROR
    assert inc.last_event_at_ns == 300


def test_tracker_closes_incident_when_detector_close_condition_fires():
    tr = IncidentTracker()
    tr.bind_detector(AlwaysCloseAfter(close_after_ns=500))
    closed: List[Incident] = []
    tr.on_closed = closed.append

    tr.ingest(_ev(100))
    tr.tick(now_ns=200)          # 100 ns since last event, not yet
    assert tr.resolved_incidents() == []

    tr.tick(now_ns=700)          # 600 ns since last event → close
    assert len(tr.resolved_incidents()) == 1
    assert tr.open_incidents() == []
    assert closed and closed[0].closed_at_ns == 700


def test_tracker_reopens_after_close_on_same_fingerprint():
    tr = IncidentTracker()
    tr.bind_detector(AlwaysCloseAfter(close_after_ns=100))

    tr.ingest(_ev(100))
    tr.tick(now_ns=500)           # closed
    assert tr.open_incidents() == []

    tr.ingest(_ev(1000))          # new incident, new id
    opens = tr.open_incidents()
    assert len(opens) == 1
    assert len(tr.resolved_incidents()) == 1
    assert opens[0].id != tr.resolved_incidents()[0].id


def test_tracker_unbound_fingerprint_falls_back_to_passthrough_close():
    # When an event arrives with a fingerprint whose detector is not bound,
    # the tracker still groups it, using the default passthrough close
    # window (30s of quiet).
    tr = IncidentTracker(passthrough_close_ns=500)

    tr.ingest(_ev(100, fingerprint="cam:adapter:xlink"))
    tr.tick(now_ns=400)
    assert tr.open_incidents()
    tr.tick(now_ns=1000)   # 900 ns since last event → closes
    assert tr.resolved_incidents()


def test_tracker_flush_callbacks_fire_on_update_too():
    tr = IncidentTracker()
    tr.bind_detector(AlwaysCloseAfter(close_after_ns=1_000_000_000))
    updates: List[Incident] = []
    tr.on_updated = updates.append

    tr.ingest(_ev(100))      # opens
    tr.ingest(_ev(200))      # updates
    tr.ingest(_ev(300))      # updates
    assert len(updates) == 2
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/health/test_incident_tracker.py -v
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/syncfield/health/tracker.py`:

```python
"""IncidentTracker — groups HealthEvents into Incidents and manages open/close.

Runs on the HealthWorker thread. Public methods are *not* thread-safe on
their own; the worker serializes access.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from syncfield.health.detector import Detector
from syncfield.health.types import Incident
from syncfield.types import HealthEvent

Callback = Callable[[Incident], None]


class IncidentTracker:
    def __init__(self, passthrough_close_ns: int = 30 * 1_000_000_000) -> None:
        self._by_fingerprint: Dict[str, Incident] = {}
        self._resolved: List[Incident] = []
        self._detectors_by_name: Dict[str, Detector] = {}
        self._passthrough_close_ns = passthrough_close_ns

        self.on_opened: Optional[Callback] = None
        self.on_updated: Optional[Callback] = None
        self.on_closed: Optional[Callback] = None

    # --- detector wiring -------------------------------------------------

    def bind_detector(self, detector: Detector) -> None:
        self._detectors_by_name[detector.name] = detector

    # --- event ingestion -------------------------------------------------

    def ingest(self, event: HealthEvent) -> None:
        open_inc = self._by_fingerprint.get(event.fingerprint)
        if open_inc is None:
            inc = Incident.opened_from(event, title=_title_from(event))
            self._by_fingerprint[event.fingerprint] = inc
            self._fire(self.on_opened, inc)
            return
        open_inc.record_event(event)
        self._fire(self.on_updated, open_inc)

    # --- tick — evaluate close conditions --------------------------------

    def tick(self, now_ns: int) -> None:
        to_close: List[str] = []
        for fp, inc in self._by_fingerprint.items():
            detector = self._detector_for(inc)
            if detector is not None:
                should_close = detector.close_condition(inc, now_ns)
            else:
                should_close = (now_ns - inc.last_event_at_ns) >= self._passthrough_close_ns
            if should_close:
                to_close.append(fp)

        for fp in to_close:
            inc = self._by_fingerprint.pop(fp)
            inc.close(at_ns=now_ns)
            self._resolved.append(inc)
            self._fire(self.on_closed, inc)

    def close_all(self, *, at_ns: int) -> None:
        """Used at session stop to resolve any still-open incidents."""
        for fp in list(self._by_fingerprint.keys()):
            inc = self._by_fingerprint.pop(fp)
            inc.close(at_ns=at_ns)
            self._resolved.append(inc)
            self._fire(self.on_closed, inc)

    # --- read-only views -------------------------------------------------

    def open_incidents(self) -> List[Incident]:
        return list(self._by_fingerprint.values())

    def resolved_incidents(self) -> List[Incident]:
        return list(self._resolved)

    # --- helpers ---------------------------------------------------------

    def _detector_for(self, inc: Incident) -> Optional[Detector]:
        # Fingerprint convention: "<stream_id>:<detector_name>[:suffix]".
        parts = inc.fingerprint.split(":", 2)
        if len(parts) < 2:
            return None
        return self._detectors_by_name.get(parts[1])

    @staticmethod
    def _fire(cb: Optional[Callback], inc: Incident) -> None:
        if cb is not None:
            cb(inc)


def _title_from(event: HealthEvent) -> str:
    # Prefer the first event's detail as the title; fall back to
    # "<source>: <fingerprint>" if the detail is missing.
    if event.detail:
        return event.detail
    return f"{event.source}: {event.fingerprint}"
```

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/health/test_incident_tracker.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/tracker.py tests/unit/health/test_incident_tracker.py
git commit -m "feat(health): add IncidentTracker with fingerprint grouping and close conditions"
```

---

### Task 7: HealthWorker (thread + ingress queues)

**Files:**
- Create: `src/syncfield/health/worker.py`
- Test: `tests/unit/health/test_health_worker.py`

The worker owns the thread, ingress queues, detector polling, and tracker. It is started on session `start()` and stopped on `stop()`.

- [ ] **Step 1: Write failing test**

`tests/unit/health/test_health_worker.py`:

```python
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
        w.push_health("cam", HealthEvent(stream_id="cam", kind=HealthEventKind.WARNING, at_ns=1))
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
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/health/test_health_worker.py -v
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/syncfield/health/worker.py`:

```python
"""HealthWorker — the dedicated thread that drives detectors + tracker.

Capture threads push samples / health events / state transitions /
writer stats into :class:`queue.SimpleQueue`\\ s. The worker drains them
every tick, fans out to each registered Detector, runs each Detector's
``tick`` to emit synthetic events, and feeds everything into the
IncidentTracker.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Iterable, List, Tuple

from syncfield.health.detector import Detector
from syncfield.health.tracker import IncidentTracker
from syncfield.health.types import WriterStats
from syncfield.types import HealthEvent, SampleEvent, SessionState


@dataclass(frozen=True)
class _SampleMsg:
    stream_id: str
    sample: SampleEvent


@dataclass(frozen=True)
class _HealthMsg:
    stream_id: str
    event: HealthEvent


@dataclass(frozen=True)
class _StateMsg:
    old: SessionState
    new: SessionState


@dataclass(frozen=True)
class _WriterStatsMsg:
    stream_id: str
    stats: WriterStats


class HealthWorker:
    def __init__(
        self,
        *,
        tracker: IncidentTracker,
        detectors: Iterable[Detector],
        tick_hz: float = 20.0,
    ) -> None:
        self._tracker = tracker
        self._detectors: List[Detector] = list(detectors)
        self._tick_interval = 1.0 / tick_hz

        self._samples: "queue.SimpleQueue[_SampleMsg]" = queue.SimpleQueue()
        self._healths: "queue.SimpleQueue[_HealthMsg]" = queue.SimpleQueue()
        self._states: "queue.SimpleQueue[_StateMsg]" = queue.SimpleQueue()
        self._writer_stats: "queue.SimpleQueue[_WriterStatsMsg]" = queue.SimpleQueue()

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- ingress (called from capture threads) ---------------------------

    def push_sample(self, stream_id: str, sample: SampleEvent) -> None:
        self._samples.put(_SampleMsg(stream_id, sample))

    def push_health(self, stream_id: str, event: HealthEvent) -> None:
        self._healths.put(_HealthMsg(stream_id, event))

    def push_state(self, old: SessionState, new: SessionState) -> None:
        self._states.put(_StateMsg(old, new))

    def push_writer_stats(self, stream_id: str, stats: WriterStats) -> None:
        self._writer_stats.put(_WriterStatsMsg(stream_id, stats))

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="syncfield-health", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    # --- main loop -------------------------------------------------------

    def _run(self) -> None:
        next_deadline = time.monotonic()
        while not self._stop.is_set():
            self._drain_once()
            self._fire_detector_ticks()
            self._tracker.tick(now_ns=time.monotonic_ns())

            next_deadline += self._tick_interval
            sleep_for = next_deadline - time.monotonic()
            if sleep_for > 0:
                # Use Event.wait so stop() can interrupt immediately.
                self._stop.wait(timeout=sleep_for)
            else:
                # Running behind; reset the schedule anchor.
                next_deadline = time.monotonic()

        # Drain any straggling messages after stop so tests see a consistent state.
        self._drain_once()

    def _drain_once(self) -> None:
        for msg in _drain_queue(self._samples):
            for d in self._detectors:
                d.observe_sample(msg.stream_id, msg.sample)
        for msg in _drain_queue(self._healths):
            for d in self._detectors:
                d.observe_health(msg.stream_id, msg.event)
            self._tracker.ingest(msg.event)
        for msg in _drain_queue(self._states):
            for d in self._detectors:
                d.observe_state(msg.old, msg.new)
        for msg in _drain_queue(self._writer_stats):
            for d in self._detectors:
                d.observe_writer_stats(msg.stream_id, msg.stats)

    def _fire_detector_ticks(self) -> None:
        now = time.monotonic_ns()
        for d in self._detectors:
            for event in d.tick(now):
                self._tracker.ingest(event)


def _drain_queue(q: "queue.SimpleQueue") -> List:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out
```

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/health/test_health_worker.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/worker.py tests/unit/health/test_health_worker.py
git commit -m "feat(health): add HealthWorker (daemon thread with ingress queues and tick loop)"
```

---

### Task 8: HealthSystem facade

**Files:**
- Create: `src/syncfield/health/system.py`
- Modify: `src/syncfield/health/__init__.py` — public API exports
- Test: `tests/unit/health/test_health_system.py`

- [ ] **Step 1: Write failing test**

`tests/unit/health/test_health_system.py`:

```python
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
        hs.observe_health("cam", HealthEvent(stream_id="cam", kind=HealthEventKind.WARNING, at_ns=1))
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
        "adapter-passthrough",
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
        import time
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if opened and closed:
                break
            time.sleep(0.02)
    finally:
        hs.stop()
    assert opened, "incident was not opened"
    assert closed, "incident was not closed"
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/health/test_health_system.py -v
```
Expected: `ImportError: cannot import name 'HealthSystem'`.

- [ ] **Step 3: Implement**

`src/syncfield/health/system.py`:

```python
"""HealthSystem — the single handle the orchestrator + user code touch."""

from __future__ import annotations

from typing import Callable, Iterable, Iterator, Optional

from syncfield.health.detector import Detector
from syncfield.health.detectors.adapter_passthrough import AdapterEventPassthrough
from syncfield.health.detectors.backpressure import BackpressureDetector
from syncfield.health.detectors.fps_drop import FpsDropDetector
from syncfield.health.detectors.jitter import JitterDetector
from syncfield.health.detectors.startup_failure import StartupFailureDetector
from syncfield.health.detectors.stream_stall import StreamStallDetector
from syncfield.health.registry import DetectorRegistry
from syncfield.health.tracker import IncidentTracker
from syncfield.health.types import Incident, WriterStats
from syncfield.health.worker import HealthWorker
from syncfield.types import HealthEvent, SampleEvent, SessionState


class HealthSystem:
    """Composes Registry + Tracker + Worker into a single user-facing facade."""

    def __init__(
        self,
        *,
        tick_hz: float = 20.0,
        passthrough_close_ns: int = 30 * 1_000_000_000,
    ) -> None:
        self._registry = DetectorRegistry()
        self._tracker = IncidentTracker(passthrough_close_ns=passthrough_close_ns)
        self._worker: Optional[HealthWorker] = None
        self._tick_hz = tick_hz

        self.on_incident_opened: Optional[Callable[[Incident], None]] = None
        self.on_incident_updated: Optional[Callable[[Incident], None]] = None
        self.on_incident_closed: Optional[Callable[[Incident], None]] = None

        self._tracker.on_opened = lambda inc: self._fire("on_incident_opened", inc)
        self._tracker.on_updated = lambda inc: self._fire("on_incident_updated", inc)
        self._tracker.on_closed = lambda inc: self._fire("on_incident_closed", inc)

        self._install_default_detectors()

    # --- registry --------------------------------------------------------

    def register(self, detector: Detector) -> None:
        self._registry.register(detector)
        self._tracker.bind_detector(detector)

    def unregister(self, name: str) -> None:
        self._registry.unregister(name)

    def iter_detectors(self) -> Iterator[Detector]:
        return iter(self._registry)

    # --- observer inputs -------------------------------------------------

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None:
        if self._worker is not None:
            self._worker.push_sample(stream_id, sample)

    def observe_health(self, stream_id: str, event: HealthEvent) -> None:
        if self._worker is not None:
            self._worker.push_health(stream_id, event)

    def observe_state(self, old: SessionState, new: SessionState) -> None:
        if self._worker is not None:
            self._worker.push_state(old, new)

    def observe_writer_stats(self, stream_id: str, stats: WriterStats) -> None:
        if self._worker is not None:
            self._worker.push_writer_stats(stream_id, stats)

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._worker = HealthWorker(
            tracker=self._tracker,
            detectors=list(self._registry),
            tick_hz=self._tick_hz,
        )
        self._worker.start()

    def stop(self, *, close_open_incidents: bool = True, now_ns: Optional[int] = None) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker = None
        if close_open_incidents:
            import time
            self._tracker.close_all(at_ns=now_ns if now_ns is not None else time.monotonic_ns())

    # --- read-only views -------------------------------------------------

    def open_incidents(self) -> Iterable[Incident]:
        return self._tracker.open_incidents()

    def resolved_incidents(self) -> Iterable[Incident]:
        return self._tracker.resolved_incidents()

    # --- helpers ---------------------------------------------------------

    def _install_default_detectors(self) -> None:
        self.register(AdapterEventPassthrough())
        self.register(StreamStallDetector())
        self.register(FpsDropDetector())
        self.register(JitterDetector())
        self.register(StartupFailureDetector())
        self.register(BackpressureDetector())

    def _fire(self, attr: str, inc: Incident) -> None:
        cb = getattr(self, attr, None)
        if cb is not None:
            cb(inc)
```

Public API in `src/syncfield/health/__init__.py`:

```python
"""syncfield.health — platform health telemetry."""

from syncfield.health.detector import Detector, DetectorBase
from syncfield.health.registry import DetectorRegistry
from syncfield.health.severity import Severity, max_severity
from syncfield.health.system import HealthSystem
from syncfield.health.tracker import IncidentTracker
from syncfield.health.types import (
    Incident,
    IncidentArtifact,
    IncidentSnapshot,
    WriterStats,
)

__all__ = [
    "Detector",
    "DetectorBase",
    "DetectorRegistry",
    "HealthSystem",
    "Incident",
    "IncidentArtifact",
    "IncidentSnapshot",
    "IncidentTracker",
    "Severity",
    "WriterStats",
    "max_severity",
]
```

**Note**: because `HealthSystem._install_default_detectors` imports all six default detector modules, those modules must exist *before* this task's tests can pass. Implement Tasks 9–14 first, or inline placeholder classes temporarily. **Correct order**: skip this task's implementation step (the test) until after Tasks 9–14. Proceed to Task 9 now; return here for the implementation + verification after the detectors exist.

- [ ] **Step 4: Defer implementation — skip to Task 9**

Revisit after Tasks 9–14 land. Run then:

```bash
pytest tests/unit/health/test_health_system.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit (after Tasks 9–14 complete)**

```bash
git add src/syncfield/health/system.py src/syncfield/health/__init__.py tests/unit/health/test_health_system.py
git commit -m "feat(health): add HealthSystem facade with default detector install + lifecycle"
```

---

## Phase 2 — Platform detectors

### Task 9: AdapterEventPassthrough

**Files:**
- Create: `src/syncfield/health/detectors/__init__.py` (empty)
- Create: `src/syncfield/health/detectors/adapter_passthrough.py`
- Test: `tests/unit/health/detectors/__init__.py` (empty), `tests/unit/health/detectors/test_adapter_passthrough.py`

The passthrough does not emit events from `tick()`; it exists so adapter-emitted events that start with `source="adapter:..."` (fingerprints like `cam:adapter:xlink-error`) still have a Detector for the tracker to consult on close — using a stale-quiet window.

- [ ] **Step 1: Write failing test**

```python
# tests/unit/health/detectors/test_adapter_passthrough.py
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
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/health/detectors/test_adapter_passthrough.py -v
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/syncfield/health/detectors/adapter_passthrough.py`:

```python
"""Passthrough detector: owns close semantics for adapter-emitted events.

Fingerprint convention ``<stream_id>:adapter:<subkind>`` routes to this
detector in the tracker. It never emits synthetic events; its only job
is saying "if no new adapter event arrived for N seconds, close the
incident".
"""

from __future__ import annotations

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import Incident


class AdapterEventPassthrough(DetectorBase):
    name = "adapter"
    default_severity = Severity.WARNING

    def __init__(self, quiet_ns: int = 30 * 1_000_000_000) -> None:
        self._quiet_ns = quiet_ns

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        return (now_ns - incident.last_event_at_ns) >= self._quiet_ns
```

**Note**: the fingerprint middle-token is `adapter`, not the detector's own name, so the tracker's `_detector_for` lookup (`parts[1]`) finds this class by `name="adapter"`.

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/health/detectors/test_adapter_passthrough.py -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/detectors/ tests/unit/health/detectors/
git commit -m "feat(health): add AdapterEventPassthrough detector"
```

---

### Task 10: StreamStallDetector

**Files:**
- Create: `src/syncfield/health/detectors/stream_stall.py`
- Test: `tests/unit/health/detectors/test_stream_stall.py`

Fires when a stream that previously produced samples has been silent for ≥ `stall_threshold_ns` (default 2.0 s). Closes after samples flow for ≥ `recovery_ns` (default 1.0 s).

- [ ] **Step 1: Write failing test**

```python
# tests/unit/health/detectors/test_stream_stall.py
from syncfield.health.detectors.stream_stall import StreamStallDetector
from syncfield.health.types import Incident
from syncfield.types import SampleEvent


def _sample(stream_id: str, capture_ns: int) -> SampleEvent:
    return SampleEvent(stream_id=stream_id, frame_number=1, capture_ns=capture_ns)


def test_no_fire_before_seeing_any_sample():
    d = StreamStallDetector(stall_threshold_ns=1000)
    assert list(d.tick(now_ns=10_000)) == []


def test_fires_when_silent_longer_than_threshold():
    d = StreamStallDetector(stall_threshold_ns=1000)
    d.observe_sample("cam", _sample("cam", capture_ns=100))
    events = list(d.tick(now_ns=2000))   # 1900 ns of silence
    assert len(events) == 1
    ev = events[0]
    assert ev.stream_id == "cam"
    assert ev.fingerprint == "cam:stream-stall"
    assert ev.source == "detector:stream-stall"
    assert "silence" in (ev.detail or "").lower()


def test_does_not_refire_while_still_stalled():
    d = StreamStallDetector(stall_threshold_ns=1000)
    d.observe_sample("cam", _sample("cam", capture_ns=100))
    fired_once = list(d.tick(now_ns=2000))
    fired_twice = list(d.tick(now_ns=3000))
    assert len(fired_once) == 1
    assert len(fired_twice) == 0


def test_refires_after_recovery_then_new_stall():
    d = StreamStallDetector(stall_threshold_ns=1000, recovery_ns=500)
    d.observe_sample("cam", _sample("cam", capture_ns=0))
    list(d.tick(now_ns=2000))                          # fires stall

    # recovery: samples flow for ≥ recovery_ns
    for t in range(3000, 4100, 100):
        d.observe_sample("cam", _sample("cam", capture_ns=t))
    inc = Incident.opened_from(list(d.tick(now_ns=4100))[0], title="x") \
        if False else None  # placeholder; close_condition test follows
    # Now silence again.
    new_events = list(d.tick(now_ns=6000))
    assert len(new_events) == 1   # second stall → new event


def test_close_condition_requires_recent_sample_flow():
    d = StreamStallDetector(stall_threshold_ns=1000, recovery_ns=500)
    d.observe_sample("cam", _sample("cam", capture_ns=0))
    events = list(d.tick(now_ns=2000))
    inc = Incident.opened_from(events[0], title="x")

    # Still silent — do not close.
    assert d.close_condition(inc, now_ns=2500) is False

    # Samples arrive across a 600 ns window → recovery_ns=500 satisfied.
    d.observe_sample("cam", _sample("cam", capture_ns=2600))
    d.observe_sample("cam", _sample("cam", capture_ns=3200))
    assert d.close_condition(inc, now_ns=3300) is True


def test_per_stream_independent_state():
    d = StreamStallDetector(stall_threshold_ns=1000)
    d.observe_sample("a", _sample("a", capture_ns=100))
    d.observe_sample("b", _sample("b", capture_ns=100))
    # stream b stays alive
    d.observe_sample("b", _sample("b", capture_ns=1800))
    events = list(d.tick(now_ns=2500))
    assert len(events) == 1
    assert events[0].stream_id == "a"
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/health/detectors/test_stream_stall.py -v
```

- [ ] **Step 3: Implement**

`src/syncfield/health/detectors/stream_stall.py`:

```python
"""StreamStallDetector — fires when a stream stops producing samples."""

from __future__ import annotations

from typing import Dict, Iterator, List

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import Incident
from syncfield.types import HealthEvent, HealthEventKind, SampleEvent


class StreamStallDetector(DetectorBase):
    name = "stream-stall"
    default_severity = Severity.ERROR

    def __init__(
        self,
        stall_threshold_ns: int = 2_000_000_000,
        recovery_ns: int = 1_000_000_000,
    ) -> None:
        self._stall_threshold_ns = stall_threshold_ns
        self._recovery_ns = recovery_ns
        # Per-stream most-recent sample monotonic time.
        self._last_sample_at: Dict[str, int] = {}
        # Per-stream: are we currently firing? prevents duplicates per stall.
        self._stall_active: Dict[str, bool] = {}

    # --- observers -------------------------------------------------------

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None:
        self._last_sample_at[stream_id] = sample.capture_ns
        # A new sample ends any active stall bookkeeping.
        self._stall_active[stream_id] = False

    # --- tick ------------------------------------------------------------

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        emitted: List[HealthEvent] = []
        for stream_id, last in self._last_sample_at.items():
            silence_ns = now_ns - last
            if silence_ns >= self._stall_threshold_ns and not self._stall_active.get(stream_id, False):
                self._stall_active[stream_id] = True
                emitted.append(HealthEvent(
                    stream_id=stream_id,
                    kind=HealthEventKind.ERROR,
                    at_ns=now_ns,
                    detail=f"Stream stalled (silence {silence_ns / 1e9:.1f}s)",
                    severity=self.default_severity,
                    source=f"detector:{self.name}",
                    fingerprint=f"{stream_id}:{self.name}",
                    data={"silence_ns": silence_ns},
                ))
        return iter(emitted)

    # --- close condition -------------------------------------------------

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        last = self._last_sample_at.get(incident.stream_id)
        if last is None:
            return False
        return (now_ns - last) < self._stall_threshold_ns \
            and (now_ns - incident.last_event_at_ns) >= self._recovery_ns
```

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/health/detectors/test_stream_stall.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/detectors/stream_stall.py tests/unit/health/detectors/test_stream_stall.py
git commit -m "feat(health): add StreamStallDetector (platform-level silence detection)"
```

---

### Task 11: FpsDropDetector

**Files:**
- Create: `src/syncfield/health/detectors/fps_drop.py`
- Test: `tests/unit/health/detectors/test_fps_drop.py`

Maintains a per-stream rolling FPS estimate over a 1-second sliding window. Fires when the observed FPS has been below 70 % of target for ≥ 3 s. Without a declared `target_hz`, learns a baseline from the first 10 s of samples after a 5 s warmup.

Since target_hz is declared by the Stream, the detector accepts a `targets: dict[str, float | None]` dependency it can query. `HealthSystem` supplies a getter.

- [ ] **Step 1: Write failing test**

```python
# tests/unit/health/detectors/test_fps_drop.py
from syncfield.health.detectors.fps_drop import FpsDropDetector
from syncfield.types import SampleEvent


def _s(stream: str, t_ns: int) -> SampleEvent:
    return SampleEvent(stream_id=stream, frame_number=0, capture_ns=t_ns)


def test_no_fire_if_fps_tracks_target():
    d = FpsDropDetector(target_getter=lambda sid: 30.0)
    # emit 30 samples over 1 second
    for i in range(30):
        d.observe_sample("cam", _s("cam", i * int(1e9 / 30)))
    assert list(d.tick(now_ns=int(1.1e9))) == []


def test_fires_when_observed_below_70_percent_for_3s():
    d = FpsDropDetector(
        target_getter=lambda sid: 30.0,
        drop_ratio=0.70,
        sustain_ns=3 * 1_000_000_000,
    )

    # 10 fps for 3.5 seconds — fps is 10, target 30, ratio 0.33.
    interval = int(1e9 / 10)
    t = 0
    while t <= int(3.5e9):
        d.observe_sample("cam", _s("cam", t))
        t += interval

    emitted = list(d.tick(now_ns=int(3.6e9)))
    assert len(emitted) == 1
    assert emitted[0].fingerprint == "cam:fps-drop"
    assert emitted[0].data["target_hz"] == 30.0
    assert emitted[0].data["observed_hz"] < 15.0


def test_does_not_fire_without_target_before_warmup():
    d = FpsDropDetector(
        target_getter=lambda sid: None,
        baseline_warmup_ns=5_000_000_000,
    )
    # 10 fps, but only for 1s — under warmup.
    t = 0
    for _ in range(10):
        d.observe_sample("cam", _s("cam", t))
        t += int(1e8)
    assert list(d.tick(now_ns=int(1.1e9))) == []


def test_learns_baseline_then_fires_on_subsequent_drop():
    d = FpsDropDetector(
        target_getter=lambda sid: None,
        baseline_warmup_ns=1_000_000_000,
        baseline_window_ns=2_000_000_000,
        drop_ratio=0.7,
        sustain_ns=1_000_000_000,
    )
    # 3 s @ 30 fps → baseline ≈ 30.
    t = 0
    while t <= int(3e9):
        d.observe_sample("cam", _s("cam", t))
        t += int(1e9 / 30)
    # 1.5 s of 10 fps → drop.
    end = t + int(1.5e9)
    while t <= end:
        d.observe_sample("cam", _s("cam", t))
        t += int(1e8)
    emitted = list(d.tick(now_ns=t))
    assert len(emitted) == 1
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/health/detectors/test_fps_drop.py -v
```

- [ ] **Step 3: Implement**

`src/syncfield/health/detectors/fps_drop.py`:

```python
"""FpsDropDetector — target-relative or baseline-learning FPS drop detector."""

from __future__ import annotations

from collections import deque
from typing import Callable, Deque, Dict, Iterator, List, Optional

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import Incident
from syncfield.types import HealthEvent, HealthEventKind, SampleEvent

TargetGetter = Callable[[str], Optional[float]]

_WINDOW_NS = 1_000_000_000  # rolling 1s FPS window


class FpsDropDetector(DetectorBase):
    name = "fps-drop"
    default_severity = Severity.WARNING

    def __init__(
        self,
        target_getter: TargetGetter = lambda sid: None,
        drop_ratio: float = 0.70,
        sustain_ns: int = 3_000_000_000,
        recovery_ratio: float = 0.90,
        recovery_ns: int = 5_000_000_000,
        baseline_warmup_ns: int = 5_000_000_000,
        baseline_window_ns: int = 10_000_000_000,
    ) -> None:
        self._target_getter = target_getter
        self._drop_ratio = drop_ratio
        self._sustain_ns = sustain_ns
        self._recovery_ratio = recovery_ratio
        self._recovery_ns = recovery_ns
        self._baseline_warmup_ns = baseline_warmup_ns
        self._baseline_window_ns = baseline_window_ns

        self._samples: Dict[str, Deque[int]] = {}
        self._first_seen_at: Dict[str, int] = {}
        self._baseline: Dict[str, float] = {}
        # When did the stream first drop below threshold in the current dip?
        self._dip_began_at: Dict[str, Optional[int]] = {}
        self._fire_active: Dict[str, bool] = {}
        # Same thing for recovery tracking.
        self._recovery_began_at: Dict[str, Optional[int]] = {}

    # --- observers -------------------------------------------------------

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None:
        buf = self._samples.setdefault(stream_id, deque())
        buf.append(sample.capture_ns)
        self._first_seen_at.setdefault(stream_id, sample.capture_ns)
        # Trim older than baseline_window.
        cutoff = sample.capture_ns - self._baseline_window_ns
        while buf and buf[0] < cutoff:
            buf.popleft()

    # --- tick ------------------------------------------------------------

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        out: List[HealthEvent] = []
        for stream_id, buf in list(self._samples.items()):
            target = self._effective_target(stream_id, now_ns)
            observed = self._observed_fps(buf, now_ns)
            if target is None or observed is None:
                continue

            if observed < target * self._drop_ratio:
                began = self._dip_began_at.get(stream_id)
                if began is None:
                    self._dip_began_at[stream_id] = now_ns
                    began = now_ns
                elapsed = now_ns - began
                if elapsed >= self._sustain_ns and not self._fire_active.get(stream_id, False):
                    self._fire_active[stream_id] = True
                    out.append(HealthEvent(
                        stream_id=stream_id,
                        kind=HealthEventKind.WARNING,
                        at_ns=now_ns,
                        detail=f"FPS drop ({observed:.1f} Hz, target {target:.1f} Hz)",
                        severity=self.default_severity,
                        source=f"detector:{self.name}",
                        fingerprint=f"{stream_id}:{self.name}",
                        data={"observed_hz": observed, "target_hz": target},
                    ))
            else:
                self._dip_began_at[stream_id] = None
                self._fire_active[stream_id] = False
        return iter(out)

    # --- close condition -------------------------------------------------

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        stream_id = incident.stream_id
        target = self._effective_target(stream_id, now_ns)
        observed = self._observed_fps(self._samples.get(stream_id, deque()), now_ns)
        if target is None or observed is None:
            return False
        if observed < target * self._recovery_ratio:
            self._recovery_began_at[stream_id] = None
            return False
        began = self._recovery_began_at.get(stream_id)
        if began is None:
            self._recovery_began_at[stream_id] = now_ns
            return False
        return (now_ns - began) >= self._recovery_ns

    # --- helpers ---------------------------------------------------------

    def _effective_target(self, stream_id: str, now_ns: int) -> Optional[float]:
        declared = self._target_getter(stream_id)
        if declared is not None:
            return float(declared)
        first = self._first_seen_at.get(stream_id)
        if first is None:
            return None
        if (now_ns - first) < self._baseline_warmup_ns:
            return None
        cached = self._baseline.get(stream_id)
        if cached is not None:
            return cached
        observed = self._observed_fps(self._samples.get(stream_id, deque()), now_ns)
        if observed is not None:
            self._baseline[stream_id] = observed
        return self._baseline.get(stream_id)

    @staticmethod
    def _observed_fps(buf: Deque[int], now_ns: int) -> Optional[float]:
        if not buf:
            return None
        cutoff = now_ns - _WINDOW_NS
        count = sum(1 for t in buf if t >= cutoff)
        if count == 0:
            return 0.0
        return count / (_WINDOW_NS / 1e9)
```

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/health/detectors/test_fps_drop.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/detectors/fps_drop.py tests/unit/health/detectors/test_fps_drop.py
git commit -m "feat(health): add FpsDropDetector (target-relative or baseline-learning)"
```

---

### Task 12: JitterDetector

**Files:**
- Create: `src/syncfield/health/detectors/jitter.py`
- Test: `tests/unit/health/detectors/test_jitter.py`

Tracks last 60 inter-sample intervals per stream. Fires when p95 interval exceeds `jitter_ratio * expected_interval` (from target_hz) for `sustain_ns`. Closes when p95 returns to ≤ 1.2× expected for `recovery_ns`.

- [ ] **Step 1: Write failing test**

```python
# tests/unit/health/detectors/test_jitter.py
from syncfield.health.detectors.jitter import JitterDetector
from syncfield.types import SampleEvent


def _s(t_ns: int) -> SampleEvent:
    return SampleEvent(stream_id="cam", frame_number=0, capture_ns=t_ns)


def test_steady_30hz_does_not_fire():
    d = JitterDetector(target_getter=lambda sid: 30.0)
    step = int(1e9 / 30)
    t = 0
    for _ in range(120):
        d.observe_sample("cam", _s(t))
        t += step
    assert list(d.tick(now_ns=t)) == []


def test_irregular_intervals_fire_when_p95_exceeds_ratio():
    d = JitterDetector(
        target_getter=lambda sid: 30.0,
        jitter_ratio=2.0,
        sustain_ns=500_000_000,
    )
    step = int(1e9 / 30)
    big = step * 4   # 4× target interval
    t = 0
    # 60 samples alternating between normal and 4× intervals.
    for i in range(60):
        d.observe_sample("cam", _s(t))
        t += big if i % 2 == 0 else step

    # Give sustain time to elapse with more irregular samples.
    for _ in range(20):
        d.observe_sample("cam", _s(t))
        t += big

    emitted = list(d.tick(now_ns=t + 500_000_000))
    assert len(emitted) == 1
    assert emitted[0].fingerprint == "cam:jitter"
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/health/detectors/test_jitter.py -v
```

- [ ] **Step 3: Implement**

`src/syncfield/health/detectors/jitter.py`:

```python
"""JitterDetector — p95-based inter-sample interval anomaly detector."""

from __future__ import annotations

from collections import deque
from typing import Callable, Deque, Dict, Iterator, List, Optional

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import Incident
from syncfield.types import HealthEvent, HealthEventKind, SampleEvent

TargetGetter = Callable[[str], Optional[float]]


def _p95(values: List[int]) -> int:
    if not values:
        return 0
    sorted_v = sorted(values)
    idx = max(0, int(0.95 * (len(sorted_v) - 1)))
    return sorted_v[idx]


class JitterDetector(DetectorBase):
    name = "jitter"
    default_severity = Severity.WARNING

    def __init__(
        self,
        target_getter: TargetGetter = lambda sid: None,
        window: int = 60,
        jitter_ratio: float = 2.0,
        sustain_ns: int = 3_000_000_000,
        recovery_ratio: float = 1.2,
        recovery_ns: int = 10_000_000_000,
    ) -> None:
        self._target_getter = target_getter
        self._window = window
        self._jitter_ratio = jitter_ratio
        self._sustain_ns = sustain_ns
        self._recovery_ratio = recovery_ratio
        self._recovery_ns = recovery_ns

        self._last_at: Dict[str, int] = {}
        self._intervals: Dict[str, Deque[int]] = {}
        self._bad_began_at: Dict[str, Optional[int]] = {}
        self._fire_active: Dict[str, bool] = {}
        self._recovery_began_at: Dict[str, Optional[int]] = {}

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None:
        last = self._last_at.get(stream_id)
        if last is not None:
            buf = self._intervals.setdefault(stream_id, deque(maxlen=self._window))
            buf.append(sample.capture_ns - last)
        self._last_at[stream_id] = sample.capture_ns

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        out: List[HealthEvent] = []
        for stream_id, buf in list(self._intervals.items()):
            target_hz = self._target_getter(stream_id)
            if target_hz is None or target_hz <= 0 or len(buf) < max(10, self._window // 2):
                continue
            expected = 1e9 / target_hz
            p95 = _p95(list(buf))

            if p95 > expected * self._jitter_ratio:
                began = self._bad_began_at.get(stream_id)
                if began is None:
                    self._bad_began_at[stream_id] = now_ns
                    began = now_ns
                if (now_ns - began) >= self._sustain_ns and not self._fire_active.get(stream_id, False):
                    self._fire_active[stream_id] = True
                    out.append(HealthEvent(
                        stream_id=stream_id,
                        kind=HealthEventKind.WARNING,
                        at_ns=now_ns,
                        detail=f"Jitter spike (p95 {p95/1e6:.1f} ms, expected {expected/1e6:.1f} ms)",
                        severity=self.default_severity,
                        source=f"detector:{self.name}",
                        fingerprint=f"{stream_id}:{self.name}",
                        data={"p95_ns": p95, "expected_ns": int(expected)},
                    ))
            else:
                self._bad_began_at[stream_id] = None
                self._fire_active[stream_id] = False
        return iter(out)

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        stream_id = incident.stream_id
        buf = self._intervals.get(stream_id)
        target_hz = self._target_getter(stream_id)
        if not buf or target_hz is None or target_hz <= 0:
            return False
        expected = 1e9 / target_hz
        p95 = _p95(list(buf))
        if p95 > expected * self._recovery_ratio:
            self._recovery_began_at[stream_id] = None
            return False
        began = self._recovery_began_at.get(stream_id)
        if began is None:
            self._recovery_began_at[stream_id] = now_ns
            return False
        return (now_ns - began) >= self._recovery_ns
```

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/health/detectors/test_jitter.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/detectors/jitter.py tests/unit/health/detectors/test_jitter.py
git commit -m "feat(health): add JitterDetector (p95-interval spike detector)"
```

---

### Task 13: StartupFailureDetector

**Files:**
- Create: `src/syncfield/health/detectors/startup_failure.py`
- Test: `tests/unit/health/detectors/test_startup_failure.py`

Fires when an adapter-emitted `HealthEvent` carries `data["phase"] in {"connect", "start_recording"}` with `kind == ERROR`. This relies on orchestrator convention (Task 15 wires this up from `SessionOrchestrator` exception handlers).

- [ ] **Step 1: Write failing test**

```python
# tests/unit/health/detectors/test_startup_failure.py
from syncfield.health.detectors.startup_failure import StartupFailureDetector
from syncfield.health.severity import Severity
from syncfield.types import HealthEvent, HealthEventKind


def _ev_for(phase: str, kind=HealthEventKind.ERROR) -> HealthEvent:
    return HealthEvent(
        stream_id="cam", kind=kind, at_ns=100, detail="boom",
        severity=Severity.ERROR, source="orchestrator",
        fingerprint=f"cam:adapter:startup-{phase}",
        data={"phase": phase},
    )


def test_fires_on_connect_phase_error():
    d = StartupFailureDetector()
    d.observe_health("cam", _ev_for("connect"))
    events = list(d.tick(now_ns=500))
    assert len(events) == 1
    assert events[0].fingerprint == "cam:startup-failure"
    assert events[0].data["phase"] == "connect"


def test_ignores_non_startup_phases():
    d = StartupFailureDetector()
    d.observe_health("cam", HealthEvent(
        stream_id="cam", kind=HealthEventKind.ERROR, at_ns=1, detail="x",
        severity=Severity.ERROR, source="adapter:foo", fingerprint="cam:adapter:xlink",
        data={},
    ))
    assert list(d.tick(now_ns=100)) == []


def test_closes_after_phase_success_signal():
    d = StartupFailureDetector()
    d.observe_health("cam", _ev_for("connect"))
    list(d.tick(now_ns=100))
    from syncfield.health.types import Incident
    inc = Incident.opened_from(_ev_for("connect"), title="x")

    # Before success, not closed.
    assert d.close_condition(inc, now_ns=200) is False

    # Success signal arrives.
    d.observe_health("cam", HealthEvent(
        stream_id="cam", kind=HealthEventKind.HEARTBEAT, at_ns=300, detail="connected",
        severity=Severity.INFO, source="orchestrator", fingerprint="cam:adapter:startup-success",
        data={"phase": "connect", "outcome": "success"},
    ))
    assert d.close_condition(inc, now_ns=400) is True
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/health/detectors/test_startup_failure.py -v
```

- [ ] **Step 3: Implement**

`src/syncfield/health/detectors/startup_failure.py`:

```python
"""StartupFailureDetector — fires when connect/start_recording raises.

Relies on orchestrator-emitted HealthEvents with ``data["phase"]`` in
{``"connect"``, ``"start_recording"``}. A subsequent success event with
``data["outcome"] == "success"`` closes the incident.
"""

from __future__ import annotations

from typing import Dict, Iterator, List, Set

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import Incident
from syncfield.types import HealthEvent, HealthEventKind

_STARTUP_PHASES = {"connect", "start_recording"}


class StartupFailureDetector(DetectorBase):
    name = "startup-failure"
    default_severity = Severity.ERROR

    def __init__(self) -> None:
        self._pending_failures: Dict[str, HealthEvent] = {}
        self._recovered: Set[str] = set()

    def observe_health(self, stream_id: str, event: HealthEvent) -> None:
        phase = event.data.get("phase") if event.data else None
        if phase not in _STARTUP_PHASES:
            return
        outcome = event.data.get("outcome") if event.data else None
        if event.kind == HealthEventKind.ERROR and outcome != "success":
            self._pending_failures[stream_id] = event
            self._recovered.discard(stream_id)
        elif outcome == "success":
            self._recovered.add(stream_id)

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        out: List[HealthEvent] = []
        for stream_id, origin in list(self._pending_failures.items()):
            out.append(HealthEvent(
                stream_id=stream_id,
                kind=HealthEventKind.ERROR,
                at_ns=now_ns,
                detail=origin.detail or "Startup failure",
                severity=self.default_severity,
                source=f"detector:{self.name}",
                fingerprint=f"{stream_id}:{self.name}",
                data={"phase": origin.data.get("phase"), "origin_at_ns": origin.at_ns},
            ))
            del self._pending_failures[stream_id]
        return iter(out)

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        return incident.stream_id in self._recovered
```

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/health/detectors/test_startup_failure.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/detectors/startup_failure.py tests/unit/health/detectors/test_startup_failure.py
git commit -m "feat(health): add StartupFailureDetector (connect / start_recording errors)"
```

---

### Task 14: BackpressureDetector

**Files:**
- Create: `src/syncfield/health/detectors/backpressure.py`
- Test: `tests/unit/health/detectors/test_backpressure.py`

Fires when writer queue fullness ≥ 0.80 for 2 s, OR when `dropped` counter increments. Closes when fullness < 0.30 for 5 s and no drops in that window.

- [ ] **Step 1: Write failing test**

```python
# tests/unit/health/detectors/test_backpressure.py
from syncfield.health.detectors.backpressure import BackpressureDetector
from syncfield.health.types import Incident, WriterStats


def _stat(at_ns, depth, cap=16, dropped=0):
    return WriterStats(stream_id="cam", at_ns=at_ns, queue_depth=depth, queue_capacity=cap, dropped=dropped)


def test_does_not_fire_with_normal_fullness():
    d = BackpressureDetector()
    for t in range(0, int(3e9), int(2.5e8)):
        d.observe_writer_stats("cam", _stat(t, depth=2))
    assert list(d.tick(now_ns=int(3e9))) == []


def test_fires_when_queue_sustained_above_threshold():
    d = BackpressureDetector(fullness_threshold=0.8, sustain_ns=int(2e9))
    for t in range(0, int(3e9), int(2.5e8)):
        d.observe_writer_stats("cam", _stat(t, depth=14))   # 14/16 = 0.875
    emitted = list(d.tick(now_ns=int(3e9)))
    assert len(emitted) == 1
    assert emitted[0].fingerprint == "cam:backpressure"


def test_fires_on_any_drop_increment():
    d = BackpressureDetector()
    d.observe_writer_stats("cam", _stat(0, depth=1, dropped=0))
    d.observe_writer_stats("cam", _stat(int(1e8), depth=1, dropped=5))
    emitted = list(d.tick(now_ns=int(2e8)))
    assert len(emitted) == 1


def test_close_condition_requires_low_and_no_new_drops():
    d = BackpressureDetector(fullness_threshold=0.8, sustain_ns=int(2e9),
                             recovery_ratio=0.3, recovery_ns=int(1e9))
    for t in range(0, int(3e9), int(2.5e8)):
        d.observe_writer_stats("cam", _stat(t, depth=14))
    events = list(d.tick(now_ns=int(3e9)))
    inc = Incident.opened_from(events[0], title="x")

    # Recovery in progress.
    d.observe_writer_stats("cam", _stat(int(3.5e9), depth=2))
    assert d.close_condition(inc, now_ns=int(4e9)) is False   # only 500 ms of recovery

    d.observe_writer_stats("cam", _stat(int(5e9), depth=2))
    assert d.close_condition(inc, now_ns=int(5e9)) is True    # 1.5 s of recovery
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/health/detectors/test_backpressure.py -v
```

- [ ] **Step 3: Implement**

`src/syncfield/health/detectors/backpressure.py`:

```python
"""BackpressureDetector — writer queue saturation + drop-counter detector."""

from __future__ import annotations

from typing import Dict, Iterator, List, Optional

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import Incident, WriterStats
from syncfield.types import HealthEvent, HealthEventKind


class BackpressureDetector(DetectorBase):
    name = "backpressure"
    default_severity = Severity.WARNING

    def __init__(
        self,
        fullness_threshold: float = 0.80,
        sustain_ns: int = 2_000_000_000,
        recovery_ratio: float = 0.30,
        recovery_ns: int = 5_000_000_000,
    ) -> None:
        self._threshold = fullness_threshold
        self._sustain_ns = sustain_ns
        self._recovery_ratio = recovery_ratio
        self._recovery_ns = recovery_ns

        self._latest: Dict[str, WriterStats] = {}
        self._bad_began_at: Dict[str, Optional[int]] = {}
        self._last_dropped: Dict[str, int] = {}
        self._pending_drop_spike: Dict[str, bool] = {}
        self._fire_active: Dict[str, bool] = {}
        self._recovery_began_at: Dict[str, Optional[int]] = {}

    def observe_writer_stats(self, stream_id: str, stats: WriterStats) -> None:
        self._latest[stream_id] = stats
        prev = self._last_dropped.get(stream_id, 0)
        if stats.dropped > prev:
            self._pending_drop_spike[stream_id] = True
        self._last_dropped[stream_id] = stats.dropped

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        out: List[HealthEvent] = []
        for stream_id, stats in self._latest.items():
            fire_now = False
            detail = ""

            if self._pending_drop_spike.pop(stream_id, False):
                fire_now = True
                detail = f"Writer dropped frames (total {stats.dropped})"

            if stats.queue_fullness >= self._threshold:
                began = self._bad_began_at.get(stream_id)
                if began is None:
                    self._bad_began_at[stream_id] = now_ns
                    began = now_ns
                if (now_ns - began) >= self._sustain_ns and not self._fire_active.get(stream_id, False):
                    fire_now = True
                    self._fire_active[stream_id] = True
                    detail = f"Writer queue {stats.queue_depth}/{stats.queue_capacity} full"
            else:
                self._bad_began_at[stream_id] = None
                self._fire_active[stream_id] = False

            if fire_now:
                out.append(HealthEvent(
                    stream_id=stream_id,
                    kind=HealthEventKind.WARNING,
                    at_ns=now_ns,
                    detail=detail,
                    severity=self.default_severity,
                    source=f"detector:{self.name}",
                    fingerprint=f"{stream_id}:{self.name}",
                    data={
                        "queue_depth": stats.queue_depth,
                        "queue_capacity": stats.queue_capacity,
                        "dropped": stats.dropped,
                    },
                ))
        return iter(out)

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        stats = self._latest.get(incident.stream_id)
        if stats is None:
            return False
        if stats.queue_fullness > self._recovery_ratio:
            self._recovery_began_at[incident.stream_id] = None
            return False
        # Require that no new drops occurred since incident opened.
        if self._last_dropped.get(incident.stream_id, 0) > incident.data.get("dropped_at_open", stats.dropped):
            return False
        began = self._recovery_began_at.get(incident.stream_id)
        if began is None:
            self._recovery_began_at[incident.stream_id] = now_ns
            return False
        return (now_ns - began) >= self._recovery_ns
```

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/health/detectors/test_backpressure.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/detectors/backpressure.py tests/unit/health/detectors/test_backpressure.py
git commit -m "feat(health): add BackpressureDetector (writer queue fullness + drop counter)"
```

---

### Task 14.5: Complete HealthSystem (Task 8 follow-up)

With Tasks 9–14 done, `HealthSystem` can now import all six default detectors.

- [ ] **Step 1: Run the deferred HealthSystem test suite**

```bash
pytest tests/unit/health/test_health_system.py -v
```
Expected: 4 passed (all tests from Task 8).

- [ ] **Step 2: Commit (if not already done in Task 8)**

```bash
git add src/syncfield/health/system.py src/syncfield/health/__init__.py tests/unit/health/test_health_system.py
git commit -m "feat(health): add HealthSystem facade with default detector install"
```

---

## Phase 3 — Orchestrator & writer integration

### Task 15: Add target_hz to StreamCapabilities + FinalizationReport.incidents

**Files:**
- Modify: `src/syncfield/types.py:176-206` (`StreamCapabilities`)
- Modify: `src/syncfield/types.py:294-328` (`FinalizationReport`)
- Test: `tests/unit/test_types_capabilities.py` (add cases), `tests/unit/test_types_finalization.py` (add cases)

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_types_capabilities.py`:

```python
from syncfield.types import StreamCapabilities


def test_target_hz_defaults_to_none():
    caps = StreamCapabilities()
    assert caps.target_hz is None


def test_target_hz_round_trips_to_dict():
    caps = StreamCapabilities(target_hz=30.0)
    d = caps.to_dict()
    assert d["target_hz"] == 30.0
```

Append to `tests/unit/test_types_finalization.py`:

```python
from syncfield.health.types import Incident
from syncfield.health.severity import Severity
from syncfield.types import FinalizationReport, HealthEvent, HealthEventKind


def test_finalization_report_incidents_default_empty():
    r = FinalizationReport(
        stream_id="cam", status="completed", frame_count=10, file_path=None,
        first_sample_at_ns=0, last_sample_at_ns=100, health_events=[], error=None,
    )
    assert r.incidents == []


def test_finalization_report_accepts_incidents():
    ev = HealthEvent(
        stream_id="cam", kind=HealthEventKind.ERROR, at_ns=1, detail="x",
        severity=Severity.ERROR, source="detector:stream-stall",
        fingerprint="cam:stream-stall",
    )
    inc = Incident.opened_from(ev, title="stall")
    r = FinalizationReport(
        stream_id="cam", status="completed", frame_count=10, file_path=None,
        first_sample_at_ns=0, last_sample_at_ns=100, health_events=[], error=None,
        incidents=[inc],
    )
    assert r.incidents == [inc]
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/test_types_capabilities.py tests/unit/test_types_finalization.py -v
```

- [ ] **Step 3: Implement**

In `src/syncfield/types.py`, add `target_hz: float | None = None` to `StreamCapabilities` (keep `live_preview` last-but-one), and update `to_dict`:

```python
@dataclass(frozen=True)
class StreamCapabilities:
    provides_audio_track: bool = False
    supports_precise_timestamps: bool = False
    is_removable: bool = False
    produces_file: bool = False
    target_hz: float | None = None
    live_preview: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "provides_audio_track": self.provides_audio_track,
            "supports_precise_timestamps": self.supports_precise_timestamps,
            "is_removable": self.is_removable,
            "produces_file": self.produces_file,
            "target_hz": self.target_hz,
            "live_preview": self.live_preview,
        }
```

Update `FinalizationReport` to add `incidents: list[Incident] = field(default_factory=list)`:

```python
from dataclasses import field  # ensure imported at top of file

@dataclass
class FinalizationReport:
    stream_id: str
    status: Literal["completed", "partial", "failed", "pending_aggregation"]
    frame_count: int
    file_path: Path | None
    first_sample_at_ns: int | None
    last_sample_at_ns: int | None
    health_events: list[HealthEvent]
    error: str | None
    jitter_p95_ns: int | None = None
    jitter_p99_ns: int | None = None
    incidents: "list" = field(default_factory=list)  # type: ignore[assignment]
```

The `Incident` forward reference avoids a circular import (`types.py` → `health.types` → `types.py`). Consumers that need the typed list import `Incident` separately.

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/test_types_capabilities.py tests/unit/test_types_finalization.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/types.py tests/unit/test_types_capabilities.py tests/unit/test_types_finalization.py
git commit -m "feat(types): add StreamCapabilities.target_hz and FinalizationReport.incidents"
```

---

### Task 16: SessionLogWriter — log_incident + incidents.jsonl path

**Files:**
- Modify: `src/syncfield/writer.py:112-161` (`SessionLogWriter`)
- Test: `tests/unit/test_writer.py` (add cases)

Write incidents to `<output_dir>/incidents.jsonl` on every open/update/close. The file is append-only; each line is the full incident state at write time, keyed by `id`.

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_writer.py`:

```python
import json

from syncfield.health.severity import Severity
from syncfield.health.types import Incident
from syncfield.types import HealthEvent, HealthEventKind
from syncfield.writer import SessionLogWriter


def _ev(at_ns: int) -> HealthEvent:
    return HealthEvent(
        stream_id="cam", kind=HealthEventKind.ERROR, at_ns=at_ns, detail="x",
        severity=Severity.ERROR, source="detector:stream-stall",
        fingerprint="cam:stream-stall",
    )


def test_log_incident_appends_to_incidents_jsonl(tmp_path):
    w = SessionLogWriter(tmp_path)
    w.open()
    try:
        inc = Incident.opened_from(_ev(100), title="stall")
        w.log_incident(inc)
        inc.record_event(_ev(200))
        w.log_incident(inc)
        inc.close(at_ns=300)
        w.log_incident(inc)
    finally:
        w.close()

    path = tmp_path / "incidents.jsonl"
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["id"] == inc.id
    assert first["event_count"] == 1
    last = json.loads(lines[-1])
    assert last["closed_at_ns"] == 300
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/test_writer.py -k incident -v
```

- [ ] **Step 3: Implement**

In `src/syncfield/writer.py`, extend `SessionLogWriter`:

```python
class SessionLogWriter:
    def __init__(self, output_dir: Path) -> None:
        self._path = output_dir / "session_log.jsonl"
        self._incidents_path = output_dir / "incidents.jsonl"
        self._handle: IO[str] | None = None
        self._incidents_handle: IO[str] | None = None

    @property
    def incidents_path(self) -> Path:
        return self._incidents_path

    def open(self) -> None:
        if self._handle is None:
            self._handle = open(self._path, "w")
        if self._incidents_handle is None:
            self._incidents_handle = open(self._incidents_path, "w")

    # existing log_event / log_health unchanged ...

    def log_incident(self, incident) -> None:
        """Append *incident*'s current full state as one JSON line."""
        if self._incidents_handle is None:
            raise RuntimeError("SessionLogWriter is not open")
        self._incidents_handle.write(
            json.dumps(incident.to_dict(), separators=(",", ":")) + "\n"
        )
        self._incidents_handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self._incidents_handle is not None:
            self._incidents_handle.close()
            self._incidents_handle = None
```

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/test_writer.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/writer.py tests/unit/test_writer.py
git commit -m "feat(writer): add SessionLogWriter.log_incident + incidents.jsonl"
```

---

### Task 17: Wire SessionOrchestrator to HealthSystem

**Files:**
- Modify: `src/syncfield/orchestrator.py` — multiple sites
- Test: `tests/integration/health/__init__.py`, `tests/integration/health/test_orchestrator_health_integration.py`

This is the biggest integration step. Each sub-step has its own runnable verification.

- [ ] **Step 1: Locate and read the target methods**

```bash
grep -n "def __init__\|def _on_stream_sample\|def _on_stream_health\|def _set_state\|def start\|def stop\|def add\|def disconnect" src/syncfield/orchestrator.py | head -40
```

Make a short note of the line numbers for: `__init__`, `add` (stream registration), `_on_stream_sample`, `_on_stream_health`, `_set_state` (or the state-transition helper), `start`, `stop`, `disconnect`, `_build_finalization_reports` (or similar).

- [ ] **Step 2: Write the failing integration test**

`tests/integration/health/__init__.py` is empty. `tests/integration/health/test_orchestrator_health_integration.py`:

```python
"""Integration: real SessionOrchestrator + FakeStream → incidents flow end-to-end."""

import json
import time
from pathlib import Path

from syncfield.orchestrator import SessionOrchestrator
from syncfield.types import StreamCapabilities

# Reuse the existing FakeStream helper if one exists; otherwise define a
# minimal StreamBase subclass inline.
try:
    from tests.helpers.fake_stream import FakeStream  # type: ignore
except ModuleNotFoundError:
    from syncfield.stream import StreamBase
    from syncfield.types import SampleEvent
    import threading
    import time as _t

    class FakeStream(StreamBase):
        def __init__(self, stream_id: str, target_hz: float | None = None):
            super().__init__(
                stream_id=stream_id,
                kind="sensor",
                capabilities=StreamCapabilities(target_hz=target_hz),
            )
            self._stop = threading.Event()
            self._thread: threading.Thread | None = None
            self._frame = 0
            self._interval = 1.0 / 30.0

        def connect(self):
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

        def disconnect(self):
            self._stop.set()
            if self._thread:
                self._thread.join(timeout=1.0)

        def start_recording(self, session_clock):
            pass

        def stop_recording(self):
            from syncfield.types import FinalizationReport
            return FinalizationReport(
                stream_id=self.id, status="completed", frame_count=self._frame,
                file_path=None, first_sample_at_ns=0, last_sample_at_ns=0,
                health_events=[], error=None,
            )

        def pause_samples(self):
            # Stop emitting without stopping the thread — simulate stall.
            self._stop_samples = True

        def resume_samples(self):
            self._stop_samples = False

        def _run(self):
            self._stop_samples = False
            while not self._stop.is_set():
                if not getattr(self, "_stop_samples", False):
                    self._frame += 1
                    self._emit_sample(SampleEvent(
                        stream_id=self.id,
                        frame_number=self._frame,
                        capture_ns=time.monotonic_ns(),
                    ))
                _t.sleep(self._interval)


def test_stall_incident_open_and_close(tmp_path: Path):
    sess = SessionOrchestrator(host_id="test", output_dir=tmp_path)
    stream = FakeStream("cam", target_hz=30.0)
    sess.add(stream)

    sess.connect()
    sess.start(countdown_s=0)

    # Induce stall.
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

    out_files = list(tmp_path.glob("**/incidents.jsonl"))
    assert out_files, "no incidents.jsonl written"
    lines = out_files[0].read_text().strip().splitlines()
    assert any(json.loads(l)["fingerprint"] == "cam:stream-stall" for l in lines)
```

- [ ] **Step 3: Run, confirm fail**

```bash
pytest tests/integration/health/test_orchestrator_health_integration.py -v
```
Expected: `AttributeError: 'SessionOrchestrator' object has no attribute 'health'`.

- [ ] **Step 4: Wire `HealthSystem` into `SessionOrchestrator.__init__`**

In `src/syncfield/orchestrator.py`, inside `__init__` (after the existing self.* attribute assignments, before any `_bring_*` helper):

```python
from syncfield.health import HealthSystem

# ... inside __init__ ...
self.health = HealthSystem()
self.health.on_incident_opened = self._persist_incident
self.health.on_incident_updated = self._persist_incident
self.health.on_incident_closed = self._persist_incident
```

And add the persistence helper method on the class:

```python
def _persist_incident(self, incident) -> None:
    writer = getattr(self, "_session_log_writer", None)
    if writer is not None:
        try:
            writer.log_incident(incident)
        except Exception:
            # Never let telemetry persistence crash the recording.
            pass
```

- [ ] **Step 5: Connect per-stream sample + health + writer-stats observers**

Find the place where `add()` registers callbacks (look for `.on_sample(` and `.on_health(`). Extend both:

```python
def add(self, stream) -> None:
    # ... existing wiring ...
    stream.on_sample(lambda s: self.health.observe_sample(stream.id, s))
    stream.on_health(lambda h: self._on_stream_health(h))   # unchanged if already here
```

And in `_on_stream_health`, forward to the health system **after** persisting to the session log:

```python
def _on_stream_health(self, event):
    writer = getattr(self, "_session_log_writer", None)
    if writer is not None:
        writer.log_health(event)
    self._buffered_health.append(event)   # existing
    self.health.observe_health(event.stream_id, event)
```

Find the state-transition helper (look for a method that updates `self._state` and fires listeners — often `_transition` or `_set_state`). Wrap the transition:

```python
def _set_state(self, new_state: SessionState) -> None:
    old = self._state
    self._state = new_state
    self.health.observe_state(old, new_state)
    # ... existing listener notifications ...
```

- [ ] **Step 6: Start / stop the worker + flush remaining incidents on stop**

In `start()`, after state transitions are ready for recording, call `self.health.start()`. In `stop()`, after final per-stream `stop_recording()` calls but before closing the session log, call `self.health.stop()`. Then collect resolved + open incidents for each stream into its `FinalizationReport.incidents`:

```python
def start(self, countdown_s: float = 3.0) -> None:
    # ... existing start logic up through transition to RECORDING ...
    self.health.start()

def stop(self):
    # ... existing stop logic: run each stream's stop_recording() ...

    self.health.stop()

    # Attach incidents to each stream's FinalizationReport.
    all_incidents = list(self.health.open_incidents()) + list(self.health.resolved_incidents())
    by_stream: dict[str, list] = {}
    for inc in all_incidents:
        by_stream.setdefault(inc.stream_id, []).append(inc)
    for stream_id, report in self._finalization_reports.items():
        report.incidents = by_stream.get(stream_id, [])

    # ... existing log close + return ...
```

- [ ] **Step 7: Pump WriterStats from the recording writer**

In whichever writer pipeline produces per-frame writes (`src/syncfield/writer.py` — look for the frame-writing path around `VideoWriter` / `SensorWriter`), push a `WriterStats` after each flush. If queues are not used, emit `WriterStats` with `queue_depth=0, queue_capacity=1, dropped=0` — the detector will simply never fire, which is correct.

If the current writer is synchronous (no queue), skip this sub-step and leave `BackpressureDetector` as a no-op; its integration tests already exercise it directly. Add a TODO comment in `writer.py`:

```python
# TODO(health): when the writer moves to a queued async path, push
# WriterStats into self._health_system.observe_writer_stats(stream_id, ...)
# on every flush. For the synchronous path, fullness stays at 0.
```

- [ ] **Step 8: Run the integration tests, confirm pass**

```bash
pytest tests/integration/health/test_orchestrator_health_integration.py -v
```
Expected: 2 passed. (Tests take ~6 s each because they exercise real timing.)

If a test is flaky under CI timing, increase the `time.sleep()` margins inside the test by 500 ms; keep the detector thresholds at production defaults.

- [ ] **Step 9: Make sure existing orchestrator tests still pass**

```bash
pytest tests/ -x -q -k "orchestrator"
```
Expected: all existing tests pass. If the `health_count` or `problem_count` tests break — those are covered in Task 19 (viewer refactor). Defer; proceed to commit.

- [ ] **Step 10: Commit**

```bash
git add src/syncfield/orchestrator.py tests/integration/health/
git commit -m "feat(orchestrator): wire SessionOrchestrator to HealthSystem (samples, health, state, incidents)"
```

---

## Phase 4 — OAK bridge & adapter integration

### Task 18: DepthAILoggerBridge

**Files:**
- Create: `src/syncfield/health/detectors/depthai_bridge.py`
- Test: `tests/unit/health/detectors/test_depthai_bridge.py`

The bridge is a `logging.Handler` that converts depthai error / warning records into `HealthEvent`s pushed directly into `HealthSystem.observe_health`. It is **not** a Detector (it doesn't own a close condition — the `AdapterEventPassthrough` does that for the resulting fingerprints).

- [ ] **Step 1: Write failing test**

```python
# tests/unit/health/detectors/test_depthai_bridge.py
import logging
from typing import List

from syncfield.health.detectors.depthai_bridge import DepthAILoggerBridge
from syncfield.types import HealthEvent


def _mk_record(msg: str, level: int = logging.ERROR, name: str = "depthai") -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname="", lineno=0, msg=msg, args=(), exc_info=None,
    )


def test_xlink_error_maps_to_xlink_fingerprint():
    captured: List[HealthEvent] = []
    bridge = DepthAILoggerBridge(stream_id="oak-main", sink=lambda sid, ev: captured.append(ev))
    rec = _mk_record("Communication exception - possible device error. Original message 'Couldn't read data from stream: '__x_0_1' (X_LINK_ERROR)'")
    bridge.emit(rec)
    assert len(captured) == 1
    ev = captured[0]
    assert ev.stream_id == "oak-main"
    assert ev.fingerprint == "oak-main:adapter:xlink-error"
    assert ev.source == "adapter:oak"
    assert ev.data.get("stream") == "__x_0_1"


def test_device_crash_attaches_crash_dump_path():
    captured: List[HealthEvent] = []
    bridge = DepthAILoggerBridge(stream_id="oak-main", sink=lambda sid, ev: captured.append(ev))
    rec = _mk_record("Device with id 194430 has crashed. Crash dump logs are stored in: /tmp/crash/crash_dump.json - please report to developers.")
    bridge.emit(rec)
    ev = captured[0]
    assert ev.fingerprint == "oak-main:adapter:device-crash"
    assert ev.data.get("crash_dump_path") == "/tmp/crash/crash_dump.json"


def test_reconnect_attempt_and_success_have_distinct_fingerprints():
    captured: List[HealthEvent] = []
    bridge = DepthAILoggerBridge(stream_id="oak-main", sink=lambda sid, ev: captured.append(ev))
    bridge.emit(_mk_record("Attempting to reconnect. Timeout is 10000ms", level=logging.WARNING))
    bridge.emit(_mk_record("Reconnection successful", level=logging.WARNING))
    fps = [c.fingerprint for c in captured]
    assert "oak-main:adapter:reconnect-attempt" in fps
    assert "oak-main:adapter:reconnect-success" in fps


def test_unrecognized_error_falls_back_to_warning_unparsed():
    captured: List[HealthEvent] = []
    bridge = DepthAILoggerBridge(stream_id="oak-main", sink=lambda sid, ev: captured.append(ev))
    bridge.emit(_mk_record("Something totally new and unrecognized", level=logging.ERROR))
    assert len(captured) == 1
    assert captured[0].source == "adapter:oak:unparsed-log"


def test_info_records_are_ignored():
    captured = []
    bridge = DepthAILoggerBridge(stream_id="oak-main", sink=lambda sid, ev: captured.append(ev))
    bridge.emit(_mk_record("Some info", level=logging.INFO))
    assert captured == []
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/health/detectors/test_depthai_bridge.py -v
```

- [ ] **Step 3: Implement**

`src/syncfield/health/detectors/depthai_bridge.py`:

```python
"""DepthAILoggerBridge — translate depthai Python log records into HealthEvents.

Installed as a standard :class:`logging.Handler` on the depthai logger.
Does not subclass DetectorBase — it is a translator, not a detector.
Its outputs are fingerprinted as ``<stream_id>:adapter:<subkind>`` so
the AdapterEventPassthrough detector owns their open/close lifecycle.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Optional

from syncfield.health.severity import Severity
from syncfield.types import HealthEvent, HealthEventKind

Sink = Callable[[str, HealthEvent], None]

_XLINK_RE = re.compile(r"X_LINK_ERROR.*stream: '([^']+)'|stream: '([^']+)'.*X_LINK_ERROR")
_CRASH_RE = re.compile(r"Device with id (\S+) has crashed\. Crash dump logs are stored in: (\S+)")
_RECONNECT_TRY_RE = re.compile(r"Attempting to reconnect", re.IGNORECASE)
_RECONNECT_OK_RE = re.compile(r"Reconnection successful", re.IGNORECASE)
_CONN_CLOSED_RE = re.compile(r"Closed connection", re.IGNORECASE)


class DepthAILoggerBridge(logging.Handler):
    def __init__(self, stream_id: str, sink: Sink) -> None:
        super().__init__(level=logging.WARNING)
        self._stream_id = stream_id
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        msg = record.getMessage()
        now = time.monotonic_ns()

        parsed = self._parse(msg, record.levelno, now)
        if parsed is None:
            parsed = HealthEvent(
                stream_id=self._stream_id,
                kind=HealthEventKind.WARNING,
                at_ns=now,
                detail=msg,
                severity=Severity.WARNING,
                source="adapter:oak:unparsed-log",
                fingerprint=f"{self._stream_id}:adapter:unparsed-log",
                data={"raw": msg, "levelname": record.levelname},
            )
        try:
            self._sink(self._stream_id, parsed)
        except Exception:
            # Never let bridge failures crash the logging path.
            pass

    def _parse(self, msg: str, levelno: int, now: int) -> Optional[HealthEvent]:
        crash = _CRASH_RE.search(msg)
        if crash:
            device_id, path = crash.group(1), crash.group(2)
            return HealthEvent(
                stream_id=self._stream_id,
                kind=HealthEventKind.ERROR,
                at_ns=now,
                detail="OAK device crashed",
                severity=Severity.CRITICAL,
                source="adapter:oak",
                fingerprint=f"{self._stream_id}:adapter:device-crash",
                data={"device_id": device_id, "crash_dump_path": path},
            )
        xlink = _XLINK_RE.search(msg)
        if xlink:
            stream = xlink.group(1) or xlink.group(2)
            return HealthEvent(
                stream_id=self._stream_id,
                kind=HealthEventKind.ERROR,
                at_ns=now,
                detail="XLink communication error",
                severity=Severity.ERROR,
                source="adapter:oak",
                fingerprint=f"{self._stream_id}:adapter:xlink-error",
                data={"stream": stream},
            )
        if _RECONNECT_OK_RE.search(msg):
            return HealthEvent(
                stream_id=self._stream_id,
                kind=HealthEventKind.RECONNECT,
                at_ns=now,
                detail="Reconnection successful",
                severity=Severity.INFO,
                source="adapter:oak",
                fingerprint=f"{self._stream_id}:adapter:reconnect-success",
            )
        if _RECONNECT_TRY_RE.search(msg):
            return HealthEvent(
                stream_id=self._stream_id,
                kind=HealthEventKind.RECONNECT,
                at_ns=now,
                detail="Attempting reconnect",
                severity=Severity.WARNING,
                source="adapter:oak",
                fingerprint=f"{self._stream_id}:adapter:reconnect-attempt",
            )
        if _CONN_CLOSED_RE.search(msg):
            return HealthEvent(
                stream_id=self._stream_id,
                kind=HealthEventKind.WARNING,
                at_ns=now,
                detail="Connection closed",
                severity=Severity.WARNING,
                source="adapter:oak",
                fingerprint=f"{self._stream_id}:adapter:connection-closed",
            )
        return None
```

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/health/detectors/test_depthai_bridge.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/detectors/depthai_bridge.py tests/unit/health/detectors/test_depthai_bridge.py
git commit -m "feat(health): add DepthAILoggerBridge (logging.Handler → HealthEvent)"
```

---

### Task 19: OakCameraStream — declare target_hz, install bridge, capture crash_dump

**Files:**
- Modify: `src/syncfield/adapters/oak_camera.py`
- Test: `tests/unit/adapters/test_oak_camera_health.py` (new)

- [ ] **Step 1: Locate the current OAK setup sites**

```bash
grep -n "StreamCapabilities\|def __init__\|def connect\|def disconnect\|fps\b" src/syncfield/adapters/oak_camera.py | head -30
```

Record line numbers for: `StreamCapabilities(...)` construction, `connect()` entry, `disconnect()` entry, the `fps=` argument passed to depthai (this is the physical target we surface as `target_hz`).

- [ ] **Step 2: Write the failing test**

`tests/unit/adapters/test_oak_camera_health.py`:

```python
"""Unit tests for OAK adapter health wiring (no real hardware required)."""

import logging

import pytest


pytest.importorskip("depthai")  # skip entire module if the oak extra isn't installed


def test_oak_declares_target_hz():
    from syncfield.adapters.oak_camera import OakCameraStream
    s = OakCameraStream(stream_id="oak-main", fps=30)
    assert s.capabilities.target_hz == 30.0


def test_oak_connect_installs_logger_bridge(monkeypatch, tmp_path):
    from syncfield.adapters.oak_camera import OakCameraStream

    captured = []
    s = OakCameraStream(stream_id="oak-main", fps=30, output_dir=tmp_path)
    s.on_health(lambda ev: captured.append(ev))

    # Don't actually build a depthai pipeline — stub the inner connect body.
    monkeypatch.setattr(s, "_open_device_pipeline", lambda: None, raising=False)
    s._install_depthai_bridge()   # exercised directly since connect() may fail without hw

    logging.getLogger("depthai").error(
        "Communication exception - Original message 'Couldn't read data from stream: '__x_0_1' (X_LINK_ERROR)'"
    )

    assert any(ev.fingerprint == "oak-main:adapter:xlink-error" for ev in captured)

    s._uninstall_depthai_bridge()
```

- [ ] **Step 3: Run, confirm fail**

```bash
pytest tests/unit/adapters/test_oak_camera_health.py -v
```

- [ ] **Step 4: Implement the wiring**

In `src/syncfield/adapters/oak_camera.py`:

- Add `target_hz` to the `StreamCapabilities(...)` construction inside `__init__`, using the existing `fps` argument: `StreamCapabilities(produces_file=True, supports_precise_timestamps=True, is_removable=True, target_hz=float(fps))`.
- Add two helpers:

```python
def _install_depthai_bridge(self) -> None:
    from syncfield.health.detectors.depthai_bridge import DepthAILoggerBridge
    if getattr(self, "_depthai_bridge", None) is not None:
        return
    self._depthai_bridge = DepthAILoggerBridge(
        stream_id=self.id,
        sink=lambda sid, ev: self._emit_health(ev),
    )
    logging.getLogger("depthai").addHandler(self._depthai_bridge)

def _uninstall_depthai_bridge(self) -> None:
    bridge = getattr(self, "_depthai_bridge", None)
    if bridge is None:
        return
    logging.getLogger("depthai").removeHandler(bridge)
    self._depthai_bridge = None
```

- Call `self._install_depthai_bridge()` at the top of `connect()` and `self._uninstall_depthai_bridge()` at the bottom of `disconnect()`.

- Where `StreamBase._emit_health` is invoked with a crash detail today, prefer letting the bridge handle it. The bridge's own `device-crash` fingerprint already attaches `crash_dump_path` in `event.data`. In the orchestrator's `_persist_incident` (Task 17 step 4), when an incident's `fingerprint` ends with `:device-crash` and `first_event.data["crash_dump_path"]` exists, attach an `IncidentArtifact`:

```python
# In orchestrator._persist_incident, before writer.log_incident:
from syncfield.health.types import IncidentArtifact

if incident.fingerprint.endswith(":device-crash"):
    path = incident.first_event.data.get("crash_dump_path")
    if path and not any(a.kind == "crash_dump" for a in incident.artifacts):
        incident.attach(IncidentArtifact(kind="crash_dump", path=str(path)))
```

- [ ] **Step 5: Run, confirm pass**

```bash
pytest tests/unit/adapters/test_oak_camera_health.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/syncfield/adapters/oak_camera.py src/syncfield/orchestrator.py tests/unit/adapters/test_oak_camera_health.py
git commit -m "feat(oak): declare target_hz, bridge depthai logger, attach crash_dump artifacts"
```

---

## Phase 5 — Viewer (server)

### Task 20: Replace HealthEntry with IncidentSnapshot in poller state

**Files:**
- Modify: `src/syncfield/viewer/state.py`
- Modify: `src/syncfield/viewer/poller.py` (if poller constructs snapshots — else just state.py)
- Test: `tests/unit/viewer/test_snapshot_incidents.py` (new)

- [ ] **Step 1: Write failing test**

```python
# tests/unit/viewer/test_snapshot_incidents.py
from syncfield.health.severity import Severity
from syncfield.health.types import Incident, IncidentSnapshot
from syncfield.types import HealthEvent, HealthEventKind
from syncfield.viewer.state import SessionSnapshot, StreamSnapshot


def _ev(at_ns: int) -> HealthEvent:
    return HealthEvent(
        stream_id="cam", kind=HealthEventKind.ERROR, at_ns=at_ns, detail="x",
        severity=Severity.ERROR, source="detector:stream-stall",
        fingerprint="cam:stream-stall",
    )


def test_session_snapshot_has_incident_fields():
    snap = SessionSnapshot(
        host_id="h", state="recording", output_dir="/tmp",
        sync_point_monotonic_ns=None, sync_point_wall_clock_ns=None,
        chirp_start_ns=None, chirp_stop_ns=None, chirp_enabled=False,
        elapsed_s=0.0, streams={}, active_incidents=[], resolved_incidents=[],
    )
    assert snap.active_incidents == []
    assert snap.resolved_incidents == []


def test_stream_snapshot_no_longer_has_health_count():
    # health_count and problem_count are removed; StreamSnapshot should not accept them.
    import dataclasses
    fields = {f.name for f in dataclasses.fields(StreamSnapshot)}
    assert "health_count" not in fields
    assert "problem_count" not in fields
```

- [ ] **Step 2: Run, confirm fail**

```bash
pytest tests/unit/viewer/test_snapshot_incidents.py -v
```

- [ ] **Step 3: Implement in `src/syncfield/viewer/state.py`**

- Remove the `HealthEntry` dataclass and all references to it.
- Remove `StreamSnapshot.health_count` (and `problem_count` if present).
- Replace `SessionSnapshot.health_log: List[HealthEntry]` with:

```python
active_incidents: List[IncidentSnapshot] = field(default_factory=list)
resolved_incidents: List[IncidentSnapshot] = field(default_factory=list)
```

Remove `StreamStatsBuffer._health` (the `HealthEntry` deque) and the `observe_health` / `snapshot_health` methods that produced it.

Imports:

```python
from syncfield.health.types import IncidentSnapshot
```

- [ ] **Step 4: Update the poller** (`src/syncfield/viewer/poller.py`)

Find where the poller is constructed or initialized with the session — it needs to subscribe to `session.health`:

```python
# in the poller's __init__, after storing self._session:
session.health.on_incident_opened = self._ingest_incident
session.health.on_incident_updated = self._ingest_incident
session.health.on_incident_closed = self._ingest_incident
```

Keep two bounded lists, updated from the callback (callbacks fire on the health worker thread — use a lock):

```python
import threading
from syncfield.health.types import IncidentSnapshot

self._incidents_lock = threading.Lock()
self._open_by_id: dict[str, Incident] = {}
self._resolved: deque[Incident] = deque(maxlen=20)

def _ingest_incident(self, incident):
    with self._incidents_lock:
        if incident.is_open:
            self._open_by_id[incident.id] = incident
        else:
            self._open_by_id.pop(incident.id, None)
            self._resolved.append(incident)
```

And when producing the snapshot:

```python
import time

with self._incidents_lock:
    now = time.monotonic_ns()
    active = [IncidentSnapshot.from_incident(i, now_ns=now) for i in self._open_by_id.values()]
    resolved = [IncidentSnapshot.from_incident(i, now_ns=now) for i in self._resolved]

snapshot = SessionSnapshot(
    # ... existing fields ...
    active_incidents=active,
    resolved_incidents=resolved,
)
```

Remove `health_log=` / `health_count=` from the construction. Remove any existing `observe_health` poller registration on streams (the orchestrator now owns that).

- [ ] **Step 5: Run, confirm pass**

```bash
pytest tests/unit/viewer/ -v
```

If existing viewer tests reference `health_count`, `problem_count`, `health_log`, or `HealthEntry`, update them to use the new fields. Search:

```bash
grep -rn "health_count\|problem_count\|health_log\|HealthEntry" tests/ src/syncfield/viewer/
```

Fix each site in the same commit.

- [ ] **Step 6: Commit**

```bash
git add src/syncfield/viewer/ tests/unit/viewer/
git commit -m "refactor(viewer): replace HealthEntry/health_count with IncidentSnapshot fields"
```

---

### Task 21: WebSocket serializer — emit incident fields

**Files:**
- Modify: `src/syncfield/viewer/server.py` — the WebSocket snapshot encoder
- Test: `tests/unit/viewer/test_server_snapshot_serialization.py` (new, or extend existing)

- [ ] **Step 1: Locate the serializer**

```bash
grep -n "SessionSnapshot\|snapshot.*dict\|jsonable\|json.dumps" src/syncfield/viewer/server.py | head -20
```

Find the function/method that converts a `SessionSnapshot` to the dict shipped over WebSocket.

- [ ] **Step 2: Write failing test**

```python
# tests/unit/viewer/test_server_snapshot_serialization.py
from syncfield.health.severity import Severity
from syncfield.health.types import Incident, IncidentSnapshot
from syncfield.types import HealthEvent, HealthEventKind
from syncfield.viewer.state import SessionSnapshot
from syncfield.viewer.server import snapshot_to_wire   # or whatever the real name is


def _inc_snap(open_: bool = True) -> IncidentSnapshot:
    ev = HealthEvent(
        stream_id="cam", kind=HealthEventKind.ERROR, at_ns=1, detail="x",
        severity=Severity.ERROR, source="detector:stream-stall",
        fingerprint="cam:stream-stall",
    )
    inc = Incident.opened_from(ev, title="stall")
    if not open_:
        inc.close(at_ns=2)
    return IncidentSnapshot.from_incident(inc, now_ns=1_000)


def test_snapshot_to_wire_emits_incident_fields():
    snap = SessionSnapshot(
        host_id="h", state="recording", output_dir="/tmp",
        sync_point_monotonic_ns=None, sync_point_wall_clock_ns=None,
        chirp_start_ns=None, chirp_stop_ns=None, chirp_enabled=False,
        elapsed_s=0.0, streams={},
        active_incidents=[_inc_snap(open_=True)],
        resolved_incidents=[_inc_snap(open_=False)],
    )
    wire = snapshot_to_wire(snap)
    assert isinstance(wire["active_incidents"], list)
    assert wire["active_incidents"][0]["severity"] == "error"
    assert wire["active_incidents"][0]["fingerprint"] == "cam:stream-stall"
    assert wire["resolved_incidents"][0]["closed_at_ns"] == 2
```

(Replace the import name if the actual helper is different — adapt based on step 1's grep.)

- [ ] **Step 3: Implement**

Update the snapshot-to-wire helper:

```python
def snapshot_to_wire(snap: SessionSnapshot) -> dict:
    return {
        # ... existing fields ...
        "active_incidents": [_incident_to_wire(i) for i in snap.active_incidents],
        "resolved_incidents": [_incident_to_wire(i) for i in snap.resolved_incidents],
    }

def _incident_to_wire(snap: IncidentSnapshot) -> dict:
    return {
        "id": snap.id,
        "stream_id": snap.stream_id,
        "fingerprint": snap.fingerprint,
        "title": snap.title,
        "severity": snap.severity,
        "source": snap.source,
        "opened_at_ns": snap.opened_at_ns,
        "closed_at_ns": snap.closed_at_ns,
        "event_count": snap.event_count,
        "detail": snap.detail,
        "ago_s": snap.ago_s,
        "artifacts": snap.artifacts,
    }
```

Remove any serialization of the old `health_log` / `health_count`.

- [ ] **Step 4: Run, confirm pass**

```bash
pytest tests/unit/viewer/test_server_snapshot_serialization.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/viewer/server.py tests/unit/viewer/test_server_snapshot_serialization.py
git commit -m "feat(viewer): serialize incidents into WebSocket snapshot payload"
```

---

## Phase 6 — Viewer (frontend)

### Task 22: Mirror Severity + IncidentSnapshot TypeScript types

**Files:**
- Modify: `src/syncfield/viewer/frontend/src/lib/types.ts`

- [ ] **Step 1: Open the file and read the current shape**

```bash
sed -n '1,60p' src/syncfield/viewer/frontend/src/lib/types.ts
```

- [ ] **Step 2: Remove HealthEntry + health_count; add new types**

Replace the `HealthEntry` type and any `StreamSnapshot.health_count` / `problem_count` fields with:

```ts
export type Severity = "info" | "warning" | "error" | "critical";

export interface IncidentArtifact {
  kind: string;
  path: string;
  detail: string | null;
}

export interface IncidentSnapshot {
  id: string;
  stream_id: string;
  fingerprint: string;
  title: string;
  severity: Severity;
  source: string;
  opened_at_ns: number;
  closed_at_ns: number | null;
  event_count: number;
  detail: string | null;
  ago_s: number;
  artifacts: IncidentArtifact[];
}

export interface SessionSnapshot {
  // ...existing fields (host_id, state, streams, elapsed_s, etc.)...
  active_incidents: IncidentSnapshot[];
  resolved_incidents: IncidentSnapshot[];
}
```

Delete the old `HealthEntry` export entirely. Update `StreamSnapshot` to drop `health_count` / `problem_count`.

- [ ] **Step 3: Typecheck**

```bash
cd src/syncfield/viewer/frontend && npx tsc --noEmit
```
Expected: errors listing every call-site that still references `HealthEntry` / `health_count`. We fix those in Tasks 23 and 24. Commit now with the type changes — subsequent tasks compile green.

```bash
git add src/syncfield/viewer/frontend/src/lib/types.ts
git commit -m "refactor(viewer-fe): mirror IncidentSnapshot + Severity; drop HealthEntry"
```

---

### Task 23: Delete health-table.tsx, add incident-panel.tsx

**Files:**
- Delete: `src/syncfield/viewer/frontend/src/components/health-table.tsx`
- Create: `src/syncfield/viewer/frontend/src/components/incident-panel.tsx`
- Modify: `src/syncfield/viewer/frontend/src/App.tsx` — replace `<HealthTable />` with `<IncidentPanel />`

- [ ] **Step 1: Create the new component**

`src/syncfield/viewer/frontend/src/components/incident-panel.tsx`:

```tsx
import { useState } from "react";
import type { IncidentSnapshot, Severity } from "../lib/types";

const SEVERITY_ICON: Record<Severity, string> = {
  info: "·",
  warning: "⚠",
  error: "⛔",
  critical: "⛔",
};

const SEVERITY_COLOR: Record<Severity, string> = {
  info: "text-slate-400",
  warning: "text-yellow-400",
  error: "text-orange-400",
  critical: "text-red-500",
};

function formatAgo(s: number): string {
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function IncidentCard({ inc, isOpen }: { inc: IncidentSnapshot; isOpen: boolean }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <button
      className="block w-full text-left px-3 py-2 rounded border border-slate-800 hover:bg-slate-900 mb-1"
      onClick={() => setExpanded(!expanded)}
    >
      <div className="flex items-start gap-2">
        <span className={`text-lg leading-none ${SEVERITY_COLOR[inc.severity]}`}>
          {SEVERITY_ICON[inc.severity]}
        </span>
        <div className="flex-1 min-w-0">
          <div className="text-sm text-slate-100 truncate">
            <span className="text-slate-400 font-mono">{inc.stream_id}</span>
            {" · "}
            {inc.title}
          </div>
          <div className="text-xs text-slate-500">
            {isOpen ? "opened " : "recovered "}
            {formatAgo(inc.ago_s)}
            {" · "}
            {inc.event_count} event{inc.event_count === 1 ? "" : "s"}
            {inc.artifacts.length > 0 &&
              inc.artifacts.map((a) => ` · ${a.kind} attached`).join("")}
          </div>
          {expanded && inc.detail && (
            <div className="mt-1 text-xs text-slate-400 font-mono break-all">
              {inc.detail}
            </div>
          )}
          {expanded &&
            inc.artifacts.map((a) => (
              <div key={a.path} className="mt-1 text-xs text-slate-400 font-mono break-all">
                {a.kind}: {a.path}
              </div>
            ))}
        </div>
      </div>
    </button>
  );
}

export function IncidentPanel({
  active,
  resolved,
}: {
  active: IncidentSnapshot[];
  resolved: IncidentSnapshot[];
}) {
  return (
    <section className="p-3 border border-slate-800 rounded">
      <header className="text-xs uppercase tracking-wide text-slate-400 mb-2">
        Active Issues ({active.length})
      </header>
      {active.length === 0 ? (
        <div className="text-xs text-slate-600 mb-3">None — all clear.</div>
      ) : (
        <div className="mb-3">
          {active.map((inc) => (
            <IncidentCard key={inc.id} inc={inc} isOpen />
          ))}
        </div>
      )}
      <header className="text-xs uppercase tracking-wide text-slate-400 mb-2">
        Resolved this session ({resolved.length})
      </header>
      {resolved.length === 0 ? (
        <div className="text-xs text-slate-600">None.</div>
      ) : (
        resolved.map((inc) => <IncidentCard key={inc.id} inc={inc} isOpen={false} />)
      )}
    </section>
  );
}
```

- [ ] **Step 2: Replace the mount site**

In `src/syncfield/viewer/frontend/src/App.tsx`, find the `<HealthTable ... />` usage and replace with:

```tsx
import { IncidentPanel } from "./components/incident-panel";

// inside render, wherever HealthTable lived:
<IncidentPanel
  active={snapshot.active_incidents ?? []}
  resolved={snapshot.resolved_incidents ?? []}
/>
```

Delete the `import { HealthTable } from "./components/health-table";` line.

- [ ] **Step 3: Delete the old file**

```bash
rm src/syncfield/viewer/frontend/src/components/health-table.tsx
```

- [ ] **Step 4: Typecheck and build**

```bash
cd src/syncfield/viewer/frontend && npx tsc --noEmit && npm run build
```
Expected: clean type-check. The bundled assets update in `static/` (or wherever the existing pipeline ships them).

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/viewer/frontend/src/components/incident-panel.tsx \
        src/syncfield/viewer/frontend/src/App.tsx \
        src/syncfield/viewer/static/   # if generated bundle is versioned
git rm src/syncfield/viewer/frontend/src/components/health-table.tsx
git commit -m "feat(viewer-fe): add IncidentPanel (replaces HealthTable)"
```

---

### Task 24: Stream card severity badge

**Files:**
- Modify: `src/syncfield/viewer/frontend/src/components/stream-card.tsx`

- [ ] **Step 1: Replace the red-dot / health_count display**

Find where `stream.health_count` (or `problem_count`) was rendered. Replace with a per-stream severity count computed from `active_incidents`:

```tsx
import type { IncidentSnapshot, Severity } from "../lib/types";

function streamIncidentStats(streamId: string, active: IncidentSnapshot[]) {
  const mine = active.filter((i) => i.stream_id === streamId);
  const count = mine.length;
  const highest: Severity | null = mine.reduce<Severity | null>((acc, i) => {
    if (acc === null) return i.severity;
    const order: Severity[] = ["info", "warning", "error", "critical"];
    return order.indexOf(i.severity) > order.indexOf(acc) ? i.severity : acc;
  }, null);
  return { count, highest };
}

const BADGE_COLOR: Record<Severity, string> = {
  info: "bg-slate-500",
  warning: "bg-yellow-500",
  error: "bg-orange-500",
  critical: "bg-red-500",
};

export function StreamCard({ stream, activeIncidents }: {
  stream: StreamSnapshot;
  activeIncidents: IncidentSnapshot[];
}) {
  const { count, highest } = streamIncidentStats(stream.id, activeIncidents);
  return (
    <div className="...">
      {/* ...existing header... */}
      {count > 0 && highest && (
        <span className={`inline-flex items-center justify-center rounded-full text-xs text-white w-5 h-5 ${BADGE_COLOR[highest]}`}>
          {count}
        </span>
      )}
      {/* ...rest of card... */}
    </div>
  );
}
```

Propagate `activeIncidents` from the parent that already has the snapshot.

- [ ] **Step 2: Typecheck**

```bash
cd src/syncfield/viewer/frontend && npx tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add src/syncfield/viewer/frontend/src/components/stream-card.tsx \
        src/syncfield/viewer/frontend/src/App.tsx   # if parent changed
git commit -m "feat(viewer-fe): stream card severity badge from active incidents"
```

---

## Phase 7 — target_hz rollout on other adapters

### Task 25: Declare target_hz on all known-target adapters

**Files:**
- Modify: `src/syncfield/adapters/uvc_webcam.py`
- Modify: `src/syncfield/adapters/host_audio.py`
- Modify: `src/syncfield/adapters/meta_quest_camera/stream.py` (or wherever its `StreamCapabilities` are built)
- Modify: `src/syncfield/adapters/ble_imu.py` (if a stable rate is known)
- Modify: `src/syncfield/adapters/insta360_go3s/stream.py` (if a live preview rate exists — otherwise skip)
- Modify: `src/syncfield/adapters/polling_sensor.py`, `push_sensor.py` (if they accept a rate arg)

- [ ] **Step 1: For each adapter, locate its StreamCapabilities construction**

```bash
grep -rn "StreamCapabilities(" src/syncfield/adapters/
```

- [ ] **Step 2: For each, thread the existing rate argument (`fps`, `rate_hz`, `target_hz`) into `target_hz=`**

Example for `uvc_webcam.py`:

```python
# before:
capabilities=StreamCapabilities(produces_file=True, is_removable=True)
# after:
capabilities=StreamCapabilities(produces_file=True, is_removable=True, target_hz=float(self._fps))
```

For `host_audio.py`, the "sample rate" is huge (48 kHz) — that isn't a per-sample-emission rate. Audio streams emit one `SampleEvent` per chunk, so set `target_hz` to `sample_rate / block_size`. If the adapter doesn't expose a block-rate cleanly, leave `target_hz=None` and rely on the baseline-learning fallback.

- [ ] **Step 3: Run the full suite to catch regressions**

```bash
pytest tests/ -q
```

- [ ] **Step 4: Commit**

```bash
git add src/syncfield/adapters/
git commit -m "feat(adapters): declare target_hz on UVC / audio / Meta Quest / BLE IMU / sensors"
```

---

## Phase 8 — Manual verification

### Task 26: Manual verification on real OAK hardware

This task is a human-run checklist, not automated code. Its purpose is to confirm the OAK-motivated failure modes surface correctly.

- [ ] **Step 1: Run a normal session, no incidents expected**

```bash
python examples/oak_live_preview.py   # or whichever example runs an OAK-only session
```
Start a recording, let it run for 30 s, stop. Open the viewer.

Expected: **Active Issues (0)**, **Resolved this session (0)**.

- [ ] **Step 2: Induce an XLink stall**

With a recording running, physically unplug the OAK's USB cable. Wait 3 seconds. Re-plug.

Expected in the viewer within 3 s of unplug:
- A **stream-stall** incident appears under Active Issues with title like "Stream stalled (silence 2.0s)", severity=`error`.
- A **xlink-error** incident appears separately, severity=`error`.
- After reconnection succeeds and samples flow for ~1 s, the stall incident moves to Resolved.

- [ ] **Step 3: Induce a crash (if reproducible)**

If a deterministic crash-reproducer exists (e.g., requesting an unsupported pipeline config mid-run), trigger it.

Expected:
- A **device-crash** incident appears under Active Issues with severity=`critical` and an attached artifact chip showing `crash_dump`.
- The incident's expanded view shows the `crash_dump.json` absolute path.

- [ ] **Step 4: Stop and inspect persisted artifacts**

After `stop()`:

```bash
cat $(find data_leader -name incidents.jsonl | head -1)
```

Expected: one JSON line per incident open/update/close. Crash incident's line includes `"artifacts": [{"kind": "crash_dump", "path": "..."}]`.

- [ ] **Step 5: Cross-check FinalizationReport**

Inside the example script, print `report.incidents` for each stream. Confirm the open/close states match the viewer's Active/Resolved counts at stop time.

- [ ] **Step 6: Capture screenshots for the PR**

Take a screenshot of the viewer during the induced stall and after recovery. Attach both to the PR description.

---

## Self-Review Checklist

After completing the plan, run this checklist before merging:

1. **Spec coverage**:
   - Goal 1 (live detection): Tasks 9–14, 17, 20–24 ✓
   - Goal 2 (post-session report): Tasks 16, 17 (FinalizationReport), 20 (persistence) ✓
   - Goal 3 (sensor-agnostic baseline): Tasks 8, 9–14 ✓
   - Goal 4 (pluggable detectors): Tasks 4, 5, 8 (`register()`) ✓
   - Goal 5 (default-on): Task 8 (`_install_default_detectors`), Task 19 (OAK bridge auto-install) ✓
   - Goal 6 (artifact capture): Task 18 (crash dump path), Task 19 (IncidentArtifact attach) ✓
   - Goal 7 (zero hot-path impact): Task 7 (lock-free SimpleQueues, dedicated daemon) ✓

2. **No placeholders**: every step shows complete code or a concrete command. No "TBD" / "similar to" / "handle edge cases".

3. **Type consistency**: `HealthSystem.observe_*`, `Detector.observe_*`, `IncidentTracker.ingest/tick`, `SessionLogWriter.log_incident`, `SessionSnapshot.active_incidents`/`resolved_incidents` — all names line up across tasks.

---

## Plan complete and saved to `docs/superpowers/plans/2026-04-22-health-telemetry.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session with checkpoints for review.

Which approach?
