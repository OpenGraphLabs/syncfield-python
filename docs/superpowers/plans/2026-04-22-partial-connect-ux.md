# Partial Connect UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the all-or-nothing `SessionOrchestrator.connect()` with per-stream partial connect, surface per-stream connection state + error in the snapshot, fire a new `NoDataDetector` for the "connected but no sample ever" case, and redesign the viewer's `StreamCard` to render state-aware overlays (connecting / waiting / failed) plus a degraded-state header chip.

**Architecture:** Orchestrator catches each `stream.connect()` independently, records a per-stream `ConnectionState` on `self._stream_states`, and emits structured `HealthEvent`s that feed the existing (previously dormant) `StartupFailureDetector`. A new `Detector.observe_connection_state` hook is added to the health protocol so `NoDataDetector` can track per-stream "entered `connected` at" timestamps. Snapshot gains two new fields; the viewer branches its `StreamCard` body on the connection state and shows a yellow `Ready (n/total)` chip when any stream is failed.

**Tech Stack:** Python 3.9+ (stdlib + existing health/ package), React + TypeScript + Tailwind for the viewer. No new external dependencies.

**Spec:** `docs/superpowers/specs/2026-04-22-partial-connect-ux-design.md`

---

## File Structure

### New (backend)

```
src/syncfield/health/detectors/no_data.py   # NoDataDetector
```

### Modified (backend)

- `src/syncfield/health/detector.py` — add `observe_connection_state` hook to Protocol + DetectorBase.
- `src/syncfield/health/worker.py` — new `_connection_states` ingress queue + fan-out to detectors.
- `src/syncfield/health/system.py` — `observe_connection_state` passthrough; register `NoDataDetector`.
- `src/syncfield/orchestrator.py` — `_stream_states` / `_stream_errors` dicts, `_set_stream_state` helper, partial-connect rewrite in `connect()`, failed-stream skip in `disconnect()`.
- `src/syncfield/viewer/state.py` — `StreamSnapshot.connection_state` / `connection_error` fields.
- `src/syncfield/viewer/poller.py` — read orchestrator's state dicts into each snapshot.
- `src/syncfield/viewer/server.py` — WebSocket serializer includes new stream fields.

### New (frontend)

```
src/syncfield/viewer/frontend/src/components/stream-overlays.tsx
```

### Modified (frontend)

- `src/syncfield/viewer/frontend/src/lib/types.ts` — `ConnectionState` type + `StreamSnapshot` field additions.
- `src/syncfield/viewer/frontend/src/components/stream-card.tsx` — branch body on `connection_state`.
- `src/syncfield/viewer/frontend/src/components/header.tsx` — degraded-state chip.

### Tests (new + extended)

```
tests/unit/health/detectors/test_no_data.py
tests/unit/health/test_worker_connection_state.py     (or add cases to test_health_worker.py)
tests/unit/test_orchestrator_partial_connect.py       (new test class)
tests/integration/health/test_partial_connect.py
tests/integration/health/test_no_data_detector.py
tests/unit/viewer/test_snapshot_connection_state.py   (or add cases to test_snapshot_incidents.py)
```

---

## Conventions

- TDD every task: failing test → confirm fail → implement → confirm pass → commit.
- Run tests via `uv run pytest <path> -v`.
- Commits follow the existing Conventional Commits style. Every commit includes the trailer `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>` (HEREDOC form).
- Implementer is agnostic to task order only within blocks marked "can be parallelized"; otherwise follow task number order.

---

## Task 1 — Add `observe_connection_state` hook to Detector protocol + base

**Files:**
- Modify: `src/syncfield/health/detector.py`
- Modify: `tests/unit/health/test_detector_base.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/health/test_detector_base.py`:

```python
def test_detector_base_observe_connection_state_default_is_noop():
    d = NoopDetector()
    # Does not raise; returns None.
    assert d.observe_connection_state("cam", "connected", 100) is None
```

- [ ] **Step 2: Run, confirm fail**

```bash
uv run pytest tests/unit/health/test_detector_base.py::test_detector_base_observe_connection_state_default_is_noop -v
```
Expected: `AttributeError: 'NoopDetector' object has no attribute 'observe_connection_state'`.

- [ ] **Step 3: Implement**

In `src/syncfield/health/detector.py`, add the method to the `Detector` Protocol (alongside the other five `observe_*` methods):

```python
    def observe_connection_state(self, stream_id: str, new_state: str, at_ns: int) -> None: ...
```

And to `DetectorBase` (as a no-op, matching the other `observe_*` defaults):

```python
    def observe_connection_state(self, stream_id: str, new_state: str, at_ns: int) -> None:
        pass
```

- [ ] **Step 4: Run, confirm pass**

```bash
uv run pytest tests/unit/health/test_detector_base.py -v
```
Expected: all tests pass (previous 3 + new 1 = 4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/detector.py tests/unit/health/test_detector_base.py
git commit -m "$(cat <<'EOF'
feat(health): add Detector.observe_connection_state hook

Adds a no-op default for the per-stream connection-state observer that
NoDataDetector will override. Protocol + base mirror the existing
observe_sample / observe_health / observe_state shape.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2 — HealthWorker: `_connection_states` ingress queue + fan-out

**Files:**
- Modify: `src/syncfield/health/worker.py`
- Modify: `tests/unit/health/test_health_worker.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/health/test_health_worker.py` (inside the existing module, at top-level):

```python
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
```

- [ ] **Step 2: Run, confirm fail**

```bash
uv run pytest tests/unit/health/test_health_worker.py::test_worker_drains_connection_state_queue_and_fans_out -v
```
Expected: `AttributeError: 'HealthWorker' object has no attribute 'push_connection_state'`.

- [ ] **Step 3: Implement**

In `src/syncfield/health/worker.py`:

1. Add a new message dataclass near the other `_*Msg`:

```python
@dataclass(frozen=True)
class _ConnectionStateMsg:
    stream_id: str
    new_state: str
    at_ns: int
```

2. Add the queue field in `HealthWorker.__init__` (alongside the existing four):

```python
        self._connection_states: "queue.SimpleQueue[_ConnectionStateMsg]" = queue.SimpleQueue()
```

3. Add the ingress method:

```python
    def push_connection_state(self, stream_id: str, new_state: str, at_ns: int) -> None:
        self._connection_states.put(_ConnectionStateMsg(stream_id, new_state, at_ns))
```

4. Extend `_drain_once` with a final block:

```python
        for msg in _drain_queue(self._connection_states):
            for d in self._detectors:
                d.observe_connection_state(msg.stream_id, msg.new_state, msg.at_ns)
```

- [ ] **Step 4: Run, confirm pass**

```bash
uv run pytest tests/unit/health/test_health_worker.py -v
```
Expected: 5 passed (previous 4 + new 1).

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/worker.py tests/unit/health/test_health_worker.py
git commit -m "$(cat <<'EOF'
feat(health): add per-stream connection-state ingress to HealthWorker

New _ConnectionStateMsg + push_connection_state() + _drain_once fan-out
so NoDataDetector (next commit) can track per-stream CONNECTING →
CONNECTED transitions via the standard observer pattern.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3 — HealthSystem: `observe_connection_state` passthrough

**Files:**
- Modify: `src/syncfield/health/system.py`
- Modify: `tests/unit/health/test_health_system.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/health/test_health_system.py`:

```python
def test_health_system_observe_connection_state_routes_to_worker():
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
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(spy.calls) < 2:
            time.sleep(0.02)
    finally:
        hs.stop()
    assert spy.calls == [("cam", "connecting", 10), ("cam", "connected", 20)]
```

- [ ] **Step 2: Run, confirm fail**

```bash
uv run pytest tests/unit/health/test_health_system.py::test_health_system_observe_connection_state_routes_to_worker -v
```
Expected: `AttributeError: 'HealthSystem' object has no attribute 'observe_connection_state'`.

- [ ] **Step 3: Implement**

In `src/syncfield/health/system.py`, add the passthrough method (right next to the other `observe_*` methods):

```python
    def observe_connection_state(self, stream_id: str, new_state: str, at_ns: int) -> None:
        if self._worker is not None:
            self._worker.push_connection_state(stream_id, new_state, at_ns)
```

- [ ] **Step 4: Run, confirm pass**

```bash
uv run pytest tests/unit/health/test_health_system.py -v
```
Expected: all existing tests + new one all pass.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/system.py tests/unit/health/test_health_system.py
git commit -m "$(cat <<'EOF'
feat(health): expose observe_connection_state on HealthSystem

Passthrough to the worker's ingress queue, matching the existing
observe_sample / observe_health / observe_state shape.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4 — NoDataDetector (core logic + unit tests)

**Files:**
- Create: `src/syncfield/health/detectors/no_data.py`
- Create: `tests/unit/health/detectors/test_no_data.py`

- [ ] **Step 1: Write failing test**

`tests/unit/health/detectors/test_no_data.py`:

```python
from syncfield.health.detectors.no_data import NoDataDetector
from syncfield.health.types import Incident
from syncfield.types import SampleEvent


def _s(stream: str, t_ns: int) -> SampleEvent:
    return SampleEvent(stream_id=stream, frame_number=0, capture_ns=t_ns)


def test_no_fire_before_threshold():
    d = NoDataDetector(threshold_ns=1000)
    d.observe_connection_state("cam", "connected", at_ns=100)
    assert list(d.tick(now_ns=500)) == []   # 400 ns elapsed, under 1000


def test_fires_after_threshold_without_sample():
    d = NoDataDetector(threshold_ns=1000)
    d.observe_connection_state("cam", "connected", at_ns=100)
    out = list(d.tick(now_ns=2000))          # 1900 ns elapsed
    assert len(out) == 1
    ev = out[0]
    assert ev.stream_id == "cam"
    assert ev.fingerprint == "cam:no-data"
    assert ev.source == "detector:no-data"
    assert "no data" in (ev.detail or "").lower()


def test_does_not_refire_while_still_no_data():
    d = NoDataDetector(threshold_ns=1000)
    d.observe_connection_state("cam", "connected", at_ns=100)
    first = list(d.tick(now_ns=2000))
    second = list(d.tick(now_ns=3000))
    assert len(first) == 1
    assert len(second) == 0


def test_close_condition_satisfied_once_sample_arrives():
    d = NoDataDetector(threshold_ns=1000)
    d.observe_connection_state("cam", "connected", at_ns=100)
    events = list(d.tick(now_ns=2000))
    inc = Incident.opened_from(events[0], title="x")

    assert d.close_condition(inc, now_ns=2100) is False   # still no sample

    d.observe_sample("cam", _s("cam", 2200))
    assert d.close_condition(inc, now_ns=2300) is True


def test_resets_bookkeeping_on_non_connected_state():
    d = NoDataDetector(threshold_ns=1000)
    d.observe_connection_state("cam", "connected", at_ns=100)
    list(d.tick(now_ns=2000))  # fires

    d.observe_connection_state("cam", "failed", at_ns=2500)
    # Back to connected → fresh clock, no duplicate fire.
    d.observe_connection_state("cam", "connected", at_ns=3000)
    assert list(d.tick(now_ns=3500)) == []   # only 500 ns since new connected


def test_per_stream_independent_state():
    d = NoDataDetector(threshold_ns=1000)
    d.observe_connection_state("a", "connected", at_ns=100)
    d.observe_connection_state("b", "connected", at_ns=100)
    d.observe_sample("b", _s("b", 200))

    out = list(d.tick(now_ns=2000))
    assert len(out) == 1
    assert out[0].stream_id == "a"
```

- [ ] **Step 2: Run, confirm fail**

```bash
uv run pytest tests/unit/health/detectors/test_no_data.py -v
```
Expected: `ModuleNotFoundError: No module named 'syncfield.health.detectors.no_data'`.

- [ ] **Step 3: Implement**

`src/syncfield/health/detectors/no_data.py`:

```python
"""NoDataDetector — fires when a stream is connected but never emits a sample.

Complements StreamStallDetector (which requires prior samples). Catches
the "connected but silent" case such as an OAK pipeline that fails to
pump frames even though its device connected. Resets bookkeeping on
any non-connected state transition so a reconnect starts a fresh clock.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Dict, List, Set

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import Incident
from syncfield.types import HealthEvent, HealthEventKind, SampleEvent


class NoDataDetector(DetectorBase):
    name = "no-data"
    default_severity = Severity.ERROR

    def __init__(self, threshold_ns: int = 5_000_000_000) -> None:
        self._threshold_ns = threshold_ns
        self._connected_at: Dict[str, int] = {}
        self._has_sample: Set[str] = set()
        self._fire_active: Dict[str, bool] = {}

    def observe_connection_state(self, stream_id: str, new_state: str, at_ns: int) -> None:
        if new_state == "connected":
            self._connected_at[stream_id] = at_ns
            self._has_sample.discard(stream_id)
            self._fire_active[stream_id] = False
        else:
            # idle / connecting / failed / disconnected → reset everything.
            self._connected_at.pop(stream_id, None)
            self._has_sample.discard(stream_id)
            self._fire_active.pop(stream_id, None)

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None:
        self._has_sample.add(stream_id)

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        out: List[HealthEvent] = []
        for stream_id, connected_at in self._connected_at.items():
            if stream_id in self._has_sample:
                continue
            elapsed = now_ns - connected_at
            if elapsed >= self._threshold_ns and not self._fire_active.get(stream_id, False):
                self._fire_active[stream_id] = True
                out.append(HealthEvent(
                    stream_id=stream_id,
                    kind=HealthEventKind.ERROR,
                    at_ns=now_ns,
                    detail=f"Connected {elapsed / 1e9:.1f}s ago but no data received",
                    severity=self.default_severity,
                    source=f"detector:{self.name}",
                    fingerprint=f"{stream_id}:{self.name}",
                    data={"connected_at_ns": connected_at, "elapsed_ns": elapsed},
                ))
        return iter(out)

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        return incident.stream_id in self._has_sample
```

- [ ] **Step 4: Run, confirm pass**

```bash
uv run pytest tests/unit/health/detectors/test_no_data.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/detectors/no_data.py tests/unit/health/detectors/test_no_data.py
git commit -m "$(cat <<'EOF'
feat(health): add NoDataDetector for connected-but-silent streams

Fires when a stream is in 'connected' state for N seconds without any
sample. Complements StreamStallDetector which requires prior samples.
Catches the OAK "black square" symptom where connect() succeeds but
the pipeline never pumps frames.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5 — Register NoDataDetector in HealthSystem default suite

**Files:**
- Modify: `src/syncfield/health/system.py`
- Modify: `tests/unit/health/test_health_system.py` (extend existing `test_health_system_installs_default_detectors`)

- [ ] **Step 1: Update the assertion**

In `tests/unit/health/test_health_system.py`, find `test_health_system_installs_default_detectors` and add `"no-data"` to the expected detector set:

```python
    for expected in (
        "adapter",
        "stream-stall",
        "fps-drop",
        "jitter",
        "startup-failure",
        "backpressure",
        "no-data",
    ):
        assert expected in names, f"missing default detector: {expected}"
```

- [ ] **Step 2: Run, confirm fail**

```bash
uv run pytest tests/unit/health/test_health_system.py::test_health_system_installs_default_detectors -v
```
Expected: `AssertionError: missing default detector: no-data`.

- [ ] **Step 3: Implement**

In `src/syncfield/health/system.py`, add the import and registration:

```python
from syncfield.health.detectors.no_data import NoDataDetector
```

Inside `_install_default_detectors`, after `self.register(BackpressureDetector())`, add:

```python
        self.register(NoDataDetector())
```

- [ ] **Step 4: Run, confirm pass**

```bash
uv run pytest tests/unit/health/test_health_system.py -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/health/system.py tests/unit/health/test_health_system.py
git commit -m "$(cat <<'EOF'
feat(health): register NoDataDetector in default suite

Now installed automatically on HealthSystem construction alongside the
other six default detectors.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6 — Orchestrator: per-stream state dicts + `_set_stream_state` helper

**Files:**
- Modify: `src/syncfield/orchestrator.py`
- Test: no dedicated test file for this task — the test lands in Task 8 (partial connect).

- [ ] **Step 1: Add state containers to `__init__`**

In `src/syncfield/orchestrator.py`, inside `SessionOrchestrator.__init__`, after the line `self._connected_streams: List[Stream] = []` (around line 360), add:

```python
        # Per-stream connection state for partial-connect semantics.
        # Keys are stream ids; values are one of:
        #   "idle" | "connecting" | "connected" | "failed" | "disconnected".
        self._stream_states: dict[str, str] = {}
        # Populated only when a stream's connect() raised.
        self._stream_errors: dict[str, str] = {}
```

- [ ] **Step 2: Add `_set_stream_state` helper**

Add a new method on the class (near the other private helpers, after `_persist_incident`):

```python
    def _set_stream_state(self, stream_id: str, new_state: str) -> None:
        """Update per-stream connection state and forward to HealthSystem.

        The health worker may not be running yet (e.g. we call this from
        add() when the session is IDLE) — observe_connection_state is a
        no-op in that case.
        """
        self._stream_states[stream_id] = new_state
        self.health.observe_connection_state(stream_id, new_state, time.monotonic_ns())
```

- [ ] **Step 3: Wire into `add()`**

Find `SessionOrchestrator.add(stream)` (around line 1456). At the end of the method body (after any existing wiring such as `stream.on_sample(...)` / `stream.on_health(...)`), add:

```python
        self._set_stream_state(stream.id, "idle")
```

- [ ] **Step 4: Quick sanity check**

```bash
uv run pytest tests/unit/test_orchestrator.py::TestAdd -v
```
Expected: all existing `TestAdd` tests still pass (no regression).

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/orchestrator.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): add per-stream connection-state dicts + helper

_stream_states and _stream_errors become the source of truth for
per-stream status. _set_stream_state is the single update point — it
also forwards to HealthSystem.observe_connection_state so NoDataDetector
sees the transitions. add() initializes each new stream to 'idle'.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7 — Orchestrator: partial-connect rewrite (happy path + error path + success signal)

**Files:**
- Modify: `src/syncfield/orchestrator.py` (the `connect()` method, around lines 1590-1655)
- Test: `tests/unit/test_orchestrator_partial_connect.py` (new)

- [ ] **Step 1: Write failing test**

`tests/unit/test_orchestrator_partial_connect.py`:

```python
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

    assert sess.state_name == SessionState.CONNECTED.value
    assert sess._stream_states["good_a"] == "connected"
    assert sess._stream_states["bad"] == "failed"
    assert sess._stream_states["good_b"] == "connected"
    assert "bad" in sess._stream_errors
    assert sess._stream_errors["bad"]  # non-empty message


def test_all_streams_failing_raises_and_returns_to_idle(tmp_path: Path):
    sess = SessionOrchestrator(host_id="h", output_dir=tmp_path)
    sess.add(FakeStream("a", fail_on_start=True))
    sess.add(FakeStream("b", fail_on_start=True))

    with pytest.raises(RuntimeError, match="no streams"):
        sess.connect()

    assert sess.state_name == SessionState.IDLE.value
    assert sess._stream_states["a"] == "failed"
    assert sess._stream_states["b"] == "failed"


def test_startup_failure_event_reaches_health_system(tmp_path: Path):
    # Spy detector that captures health events it observes.
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

    # Give the worker a tick to drain the health queue.
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
```

- [ ] **Step 2: Run, confirm fail**

```bash
uv run pytest tests/unit/test_orchestrator_partial_connect.py -v
```
Expected: multiple failures — the first test hits the current all-or-nothing rollback and the session ends up in IDLE.

- [ ] **Step 3: Implement**

In `src/syncfield/orchestrator.py`, replace the existing `connect()` body's try/except block (lines 1623-1643 in the current file — find the section starting `connected: List[Stream] = []` and ending just before `self._connected_streams = connected`) with:

```python
            connected: List[Stream] = []
            for stream in self._streams.values():
                self._set_stream_state(stream.id, "connecting")
                try:
                    stream.prepare()
                    stream.connect()
                except Exception as exc:
                    self._stream_errors[stream.id] = str(exc)
                    self._set_stream_state(stream.id, "failed")
                    stream._emit_health(HealthEvent(
                        stream_id=stream.id,
                        kind=HealthEventKind.ERROR,
                        at_ns=time.monotonic_ns(),
                        detail=str(exc),
                        severity=Severity.ERROR,
                        source="orchestrator",
                        fingerprint=f"{stream.id}:startup-failure",
                        data={"phase": "connect", "outcome": "error", "error": str(exc)},
                    ))
                    continue
                connected.append(stream)
                self._stream_errors.pop(stream.id, None)
                self._set_stream_state(stream.id, "connected")
                stream._emit_health(HealthEvent(
                    stream_id=stream.id,
                    kind=HealthEventKind.HEARTBEAT,
                    at_ns=time.monotonic_ns(),
                    detail="connected",
                    severity=Severity.INFO,
                    source="orchestrator",
                    fingerprint=f"{stream.id}:startup-success",
                    data={"phase": "connect", "outcome": "success"},
                ))

            if not connected:
                self._transition(SessionState.IDLE)
                if self._log_writer is not None:
                    self._log_writer.close()
                    self._log_writer = None
                raise RuntimeError(
                    "connect() failed: no streams connected — every adapter raised. "
                    "Inspect per-stream errors via session._stream_errors."
                )
```

Imports at the top of the file need to include `Severity` if not already present:

```python
from syncfield.health.severity import Severity
```

(`HealthEvent` and `HealthEventKind` should already be imported.)

- [ ] **Step 4: Run, confirm pass**

```bash
uv run pytest tests/unit/test_orchestrator_partial_connect.py -v
```
Expected: 3 passed.

Also run the full orchestrator suite to surface regressions:

```bash
uv run pytest tests/unit/test_orchestrator.py -v
```
Expected: all pre-existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/orchestrator.py tests/unit/test_orchestrator_partial_connect.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): partial-connect — survive per-stream connect() failure

One stream raising no longer rolls the whole session back to IDLE. We
record the error, emit a structured HealthEvent (phase=connect,
outcome=error) that activates the previously-dormant
StartupFailureDetector, and continue with the rest. Session only fails
if every adapter raises. Success path emits the complementary
outcome=success signal so the detector's recovery tracking works.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8 — Orchestrator: `disconnect()` skips failed streams, updates state

**Files:**
- Modify: `src/syncfield/orchestrator.py` (the `disconnect()` method around line 2100)
- Test: `tests/unit/test_orchestrator_partial_connect.py` (extend)

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_orchestrator_partial_connect.py`:

```python
def test_disconnect_does_not_call_stream_disconnect_on_failed(tmp_path: Path):
    from syncfield.testing import FakeStream

    class CountingFakeStream(FakeStream):
        def __init__(self, stream_id, fail_on_start=False):
            super().__init__(stream_id, fail_on_start=fail_on_start)
            self.disconnect_calls = 0

        def disconnect(self):
            self.disconnect_calls += 1
            super().disconnect()

    sess = SessionOrchestrator(host_id="h", output_dir=tmp_path)
    good = CountingFakeStream("good")
    bad = CountingFakeStream("bad", fail_on_start=True)
    sess.add(good)
    sess.add(bad)

    sess.connect()
    sess.disconnect()

    assert good.disconnect_calls == 1
    assert bad.disconnect_calls == 0
    assert sess._stream_states["good"] == "disconnected"
    assert sess._stream_states["bad"] == "disconnected"
    assert sess._stream_errors == {}
```

- [ ] **Step 2: Run, confirm fail**

```bash
uv run pytest tests/unit/test_orchestrator_partial_connect.py::test_disconnect_does_not_call_stream_disconnect_on_failed -v
```
Expected: at minimum `sess._stream_errors != {}` (errors never cleared), or `bad.disconnect_calls > 0` because the rollback helper iterates `_connected_streams` — which is correct for good, but we also need to flip each stream's state.

- [ ] **Step 3: Implement**

Locate `SessionOrchestrator.disconnect()` (around line 2100). Replace the body of the `with self._lock:` block (the part that calls `_rollback_disconnect_streams(self._connected_streams)`) with:

```python
            if self._state not in (SessionState.CONNECTED, SessionState.STOPPED):
                raise RuntimeError(
                    f"disconnect() requires CONNECTED or STOPPED state; "
                    f"current state is {self._state.value}"
                )
            # Only streams that were 'connected' (or 'recording' → 'stopped')
            # ever opened hardware; 'failed' streams never did, so skip them.
            _rollback_disconnect_streams(self._connected_streams)
            self._connected_streams = []

            # Flip every stream's snapshot-visible state to 'disconnected',
            # regardless of whether we called disconnect() on it. Clear any
            # per-stream errors since the session is returning to a clean slate.
            for stream_id in list(self._stream_states.keys()):
                self._set_stream_state(stream_id, "disconnected")
            self._stream_errors.clear()

            # Keep auto-injected audio stream registered (visible in viewer)
            # but disconnected. It will be reconnected on next connect().

            # Multi-host infrastructure (advertiser, browser, control plane)
            # stays up across disconnect(). It was brought up at __init__
            # and is only torn down by shutdown() or the atexit handler.
```

- [ ] **Step 4: Run, confirm pass**

```bash
uv run pytest tests/unit/test_orchestrator_partial_connect.py -v
```
Expected: 4 passed.

```bash
uv run pytest tests/unit/test_orchestrator.py -v
```
Expected: no regression.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/orchestrator.py tests/unit/test_orchestrator_partial_connect.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): disconnect() handles partial-connect survivors

_connected_streams only ever held successful streams, so the existing
rollback helper is already correct — but we now flip every tracked
stream's state to 'disconnected' and clear per-stream errors so the
viewer reflects a clean slate after teardown.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9 — `StreamSnapshot.connection_state` + `connection_error` fields

**Files:**
- Modify: `src/syncfield/viewer/state.py`
- Test: `tests/unit/viewer/test_snapshot_incidents.py` (extend)

- [ ] **Step 1: Write failing test**

Append to `tests/unit/viewer/test_snapshot_incidents.py`:

```python
def test_stream_snapshot_has_connection_state_fields():
    import dataclasses
    from syncfield.viewer.state import StreamSnapshot

    fields = {f.name: f for f in dataclasses.fields(StreamSnapshot)}
    assert "connection_state" in fields
    assert "connection_error" in fields

    # Defaults when constructed minimally.
    snap = StreamSnapshot(
        id="cam", kind="video", provides_audio_track=False, produces_file=False,
        frame_count=0, last_sample_at_ns=None, effective_hz=0.0,
        latest_frame=None, plot_points={}, latest_pose={},
    )
    assert snap.connection_state == "idle"
    assert snap.connection_error is None
```

- [ ] **Step 2: Run, confirm fail**

```bash
uv run pytest tests/unit/viewer/test_snapshot_incidents.py::test_stream_snapshot_has_connection_state_fields -v
```
Expected: `AssertionError: 'connection_state' not in fields`.

- [ ] **Step 3: Implement**

In `src/syncfield/viewer/state.py`, find the `StreamSnapshot` dataclass. Add two new fields **at the end** of the dataclass (after `live_preview: bool = True`), so constructors that supply only positional arguments for existing fields keep working:

```python
    connection_state: str = "idle"
    connection_error: Optional[str] = None
```

Ensure `Optional` is imported (it already is in that file).

- [ ] **Step 4: Run, confirm pass**

```bash
uv run pytest tests/unit/viewer/test_snapshot_incidents.py -v
```
Expected: all pass.

Regression check:

```bash
uv run pytest tests/unit/viewer -v
```

If any test constructs `StreamSnapshot` positionally and breaks — fix inline by switching to keyword arguments in that test. Add a note to the commit message if so.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/viewer/state.py tests/unit/viewer/test_snapshot_incidents.py
git commit -m "$(cat <<'EOF'
feat(viewer): add connection_state / connection_error to StreamSnapshot

Defaults ('idle' / None) preserve existing behavior for any construction
that doesn't set them. Poller (next task) wires the orchestrator's per-
stream state into these fields.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10 — Poller: propagate orchestrator state into snapshot

**Files:**
- Modify: `src/syncfield/viewer/poller.py`
- Test: `tests/unit/viewer/test_poller.py` (extend)

- [ ] **Step 1: Write failing test**

Append to `tests/unit/viewer/test_poller.py` (use the existing test harness pattern — find how other tests construct a session and poller):

```python
def test_poller_snapshot_includes_connection_state(tmp_path):
    from syncfield.orchestrator import SessionOrchestrator
    from syncfield.testing import FakeStream
    from syncfield.viewer.poller import SessionPoller

    sess = SessionOrchestrator(host_id="h", output_dir=tmp_path)
    sess.add(FakeStream("good"))
    sess.add(FakeStream("bad", fail_on_start=True))

    poller = SessionPoller(sess)
    sess.connect()

    snap = poller.snapshot()
    assert snap.streams["good"].connection_state == "connected"
    assert snap.streams["good"].connection_error is None
    assert snap.streams["bad"].connection_state == "failed"
    assert snap.streams["bad"].connection_error  # non-empty
```

- [ ] **Step 2: Run, confirm fail**

```bash
uv run pytest tests/unit/viewer/test_poller.py::test_poller_snapshot_includes_connection_state -v
```
Expected: `AssertionError` on `connection_state == "connected"` (current default is `"idle"` because the poller doesn't set it).

- [ ] **Step 3: Implement**

In `src/syncfield/viewer/poller.py`, locate the method that builds per-stream `StreamSnapshot` objects (look for `StreamSnapshot(` inside `_build_snapshot` or equivalent). Add the two new fields to the constructor call, pulling from the orchestrator:

```python
            connection_state=self._session._stream_states.get(stream.id, "idle"),
            connection_error=self._session._stream_errors.get(stream.id),
```

If the poller holds the session under a different attribute name (e.g. `self._orchestrator`), use that — read the surrounding code.

- [ ] **Step 4: Run, confirm pass**

```bash
uv run pytest tests/unit/viewer/test_poller.py -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/viewer/poller.py tests/unit/viewer/test_poller.py
git commit -m "$(cat <<'EOF'
feat(viewer): poller reads orchestrator stream state into snapshot

Every per-stream StreamSnapshot now carries connection_state and
connection_error, sourced from the orchestrator's per-stream dicts
populated by _set_stream_state.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11 — Server: serialize new stream fields onto the wire

**Files:**
- Modify: `src/syncfield/viewer/server.py`
- Test: `tests/unit/test_viewer_aggregation_snapshot.py` (extend) or `tests/unit/viewer/test_server_snapshot_serialization.py` (create if missing)

- [ ] **Step 1: Locate the serializer**

```bash
grep -n "snapshot_to_dict\|def _to_wire\|snapshot\.streams\|_serialize_stream" src/syncfield/viewer/server.py | head -10
```

Record the function name + file location. Usually a helper that maps each `StreamSnapshot` to a JSON-friendly dict.

- [ ] **Step 2: Write failing test**

Append to `tests/unit/test_viewer_aggregation_snapshot.py` (it already has a `_make_snapshot_mock()` helper from the Task 20 refactor):

```python
def test_serialized_stream_includes_connection_state():
    from syncfield.viewer.server import snapshot_to_dict   # or whatever name
    from syncfield.viewer.state import StreamSnapshot, SessionSnapshot

    stream_snap = StreamSnapshot(
        id="cam", kind="video", provides_audio_track=False, produces_file=False,
        frame_count=0, last_sample_at_ns=None, effective_hz=0.0,
        latest_frame=None, plot_points={}, latest_pose={},
        connection_state="failed", connection_error="Device not visible",
    )
    sess_snap = SessionSnapshot(
        host_id="h", state="idle", output_dir="/tmp",
        sync_point_monotonic_ns=None, sync_point_wall_clock_ns=None,
        chirp_start_ns=None, chirp_stop_ns=None, chirp_enabled=False,
        elapsed_s=0.0, streams={"cam": stream_snap},
        active_incidents=[], resolved_incidents=[],
    )
    out = snapshot_to_dict(sess_snap)
    assert out["streams"]["cam"]["connection_state"] == "failed"
    assert out["streams"]["cam"]["connection_error"] == "Device not visible"
```

(Adapt `snapshot_to_dict` to the actual function name discovered in Step 1.)

- [ ] **Step 3: Run, confirm fail**

```bash
uv run pytest tests/unit/test_viewer_aggregation_snapshot.py -k connection_state -v
```
Expected: `KeyError` or missing field.

- [ ] **Step 4: Implement**

In `src/syncfield/viewer/server.py`, in the per-stream serializer helper, add the two keys next to the existing `frame_count` / `effective_hz` keys:

```python
            "connection_state": stream.connection_state,
            "connection_error": stream.connection_error,
```

- [ ] **Step 5: Run, confirm pass**

```bash
uv run pytest tests/unit -q --timeout 30
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/syncfield/viewer/server.py tests/unit/test_viewer_aggregation_snapshot.py
git commit -m "$(cat <<'EOF'
feat(viewer): emit connection_state / connection_error over WebSocket

Frontend consumes these to select the right StreamCard overlay and to
populate the degraded-state header chip.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12 — Frontend TypeScript types

**Files:**
- Modify: `src/syncfield/viewer/frontend/src/lib/types.ts`

- [ ] **Step 1: Add the ConnectionState type + fields**

Add near the existing `Severity` type declaration:

```ts
export type ConnectionState =
  | "idle"
  | "connecting"
  | "connected"
  | "failed"
  | "disconnected";
```

In the `StreamSnapshot` interface (the one mirroring the Python dataclass), add at the end:

```ts
  connection_state: ConnectionState;
  connection_error: string | null;
```

- [ ] **Step 2: Typecheck**

```bash
cd src/syncfield/viewer/frontend && npx tsc --noEmit
```
Expected: clean typecheck (no errors unless downstream code is already missing a field — we fix those in Tasks 13-15).

- [ ] **Step 3: Commit**

```bash
git add src/syncfield/viewer/frontend/src/lib/types.ts
git commit -m "$(cat <<'EOF'
feat(viewer-fe): mirror ConnectionState + StreamSnapshot additions

ConnectionState enum and two new fields on StreamSnapshot so the
overlay branching in StreamCard (next commit) is type-safe.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13 — Frontend overlay components

**Files:**
- Create: `src/syncfield/viewer/frontend/src/components/stream-overlays.tsx`

- [ ] **Step 1: Create the component file**

```tsx
import { useState } from "react";

export function ConnectingOverlay() {
  return (
    <div className="flex items-center justify-center w-full aspect-video bg-slate-900/60 border border-slate-800 rounded">
      <div className="flex items-center gap-2 text-slate-300 text-sm">
        <span className="inline-block w-2 h-2 rounded-full bg-slate-400 animate-pulse" />
        Connecting…
      </div>
    </div>
  );
}

export function WaitingForDataOverlay() {
  return (
    <div className="flex items-center justify-center w-full aspect-video bg-yellow-900/20 border border-yellow-700/40 rounded">
      <div className="text-yellow-200 text-sm">
        Connected · waiting for first frame
      </div>
    </div>
  );
}

export function FailedOverlay({ error }: { error: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <button
      onClick={() => setExpanded((v) => !v)}
      className="w-full aspect-video bg-red-950/40 border border-red-500/40 rounded p-3 text-left cursor-pointer"
    >
      <div className="flex items-start gap-2">
        <span className="text-red-400 text-lg leading-none">⛔</span>
        <div className="flex-1 min-w-0">
          <div className="text-red-200 text-sm font-medium">
            Failed to connect
          </div>
          <div
            className={
              "mt-1 text-xs font-mono text-red-200/80 break-all " +
              (expanded ? "" : "line-clamp-2")
            }
          >
            {error}
          </div>
          <div className="mt-2 text-[11px] text-red-300/60">
            Press Discover Devices, or Disconnect + Connect to retry.
          </div>
        </div>
      </div>
    </button>
  );
}
```

- [ ] **Step 2: Typecheck + build**

```bash
cd src/syncfield/viewer/frontend && npx tsc --noEmit && npm run build
```
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add src/syncfield/viewer/frontend/src/components/stream-overlays.tsx \
        src/syncfield/viewer/frontend/src/lib/types.ts \
        src/syncfield/viewer/static/   # if the build emits updated bundle (skip if gitignored)
# Drop the static/ line above if the working tree shows no changes there.
git commit -m "$(cat <<'EOF'
feat(viewer-fe): add Connecting / WaitingForData / Failed overlays

Three small presentational components used by StreamCard (next commit)
to replace the browser broken-image fallback. FailedOverlay is click-
to-expand for long error messages.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Note: `src/syncfield/viewer/static/` is gitignored; the build output stays local. Do not force-add it.

---

## Task 14 — StreamCard: branch body on `connection_state`

**Files:**
- Modify: `src/syncfield/viewer/frontend/src/components/stream-card.tsx`

- [ ] **Step 1: Import overlays**

Add near the top of the file:

```tsx
import {
  ConnectingOverlay,
  WaitingForDataOverlay,
  FailedOverlay,
} from "./stream-overlays";
```

- [ ] **Step 2: Insert body branching**

Find the part of `StreamCard` that currently renders the video `<img>` / `VideoPreview`. Wrap it so the branch decides what to render:

```tsx
function StreamCardBody({ stream }: { stream: StreamSnapshot }) {
  if (stream.connection_state === "connecting") {
    return <ConnectingOverlay />;
  }
  if (stream.connection_state === "failed") {
    return <FailedOverlay error={stream.connection_error ?? "Unknown error"} />;
  }
  if (stream.connection_state === "connected" && stream.frame_count === 0 && stream.kind === "video") {
    return <WaitingForDataOverlay />;
  }
  // Existing render path (VideoPreview / SensorChart / AudioLevel / etc.)
  return <ExistingStreamBody stream={stream} />;  // adapt to real name
}
```

Replace the existing `<img>` / `<VideoPreview>` usage inside `StreamCard` with `<StreamCardBody stream={stream} />`. If `StreamCard` inlines the video element rather than delegating, extract the pre-existing body into a small helper named `ExistingStreamBody` first so the branch above works cleanly.

- [ ] **Step 3: Typecheck + visual smoke test**

```bash
cd src/syncfield/viewer/frontend && npx tsc --noEmit && npm run build
```

Manual smoke (optional if you have the dev server running):

```bash
# terminal 1
cd src/syncfield/viewer/frontend && npm run dev
# terminal 2
python examples/mac_iphone_dual_oak/record.py
```
Press Connect. Expected visual:
- `mac_webcam` / `iphone` / `host_audio` → normal body (video or audio chart)
- `oak_lite` / `oak_d` → red "Failed to connect" overlay with the depthai error message

- [ ] **Step 4: Commit**

```bash
git add src/syncfield/viewer/frontend/src/components/stream-card.tsx
git commit -m "$(cat <<'EOF'
feat(viewer-fe): branch StreamCard body on connection_state

Connecting / waiting-for-first-frame / failed now render explicit
overlays instead of the browser's broken-image placeholder. Healthy
streams still render the existing video / sensor / audio body.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 15 — Header: degraded-state chip

**Files:**
- Modify: `src/syncfield/viewer/frontend/src/components/header.tsx`

- [ ] **Step 1: Locate the chip**

```bash
grep -n "Ready\|SessionState\|state.*chip\|state.*label" src/syncfield/viewer/frontend/src/components/header.tsx | head
```

Find where the state label (`Ready` / `Recording` / …) is composed.

- [ ] **Step 2: Add the counter logic**

At the top of the component, after the snapshot prop is destructured:

```tsx
const streams = Object.values(snapshot.streams);
const total = streams.length;
const connected = streams.filter((s) => s.connection_state === "connected").length;
const showCount = total > 0 && connected < total;
const label = showCount ? `${stateLabel} (${connected}/${total})` : stateLabel;
const chipTone = showCount ? "warning" : "normal";
```

Use `chipTone` to choose the className. If the header currently has a Tailwind class like `bg-emerald-500/10`, add:

```tsx
const toneClass = chipTone === "warning"
  ? "bg-yellow-500/15 text-yellow-300 border border-yellow-500/40"
  : "bg-emerald-500/10 text-emerald-300 border border-emerald-500/30";
```

Apply `toneClass` to the chip's container element, and render `{label}` instead of `stateLabel` inside it.

- [ ] **Step 3: Typecheck + build**

```bash
cd src/syncfield/viewer/frontend && npx tsc --noEmit && npm run build
```

- [ ] **Step 4: Commit**

```bash
git add src/syncfield/viewer/frontend/src/components/header.tsx
git commit -m "$(cat <<'EOF'
feat(viewer-fe): show degraded counter on header state chip

'Ready (3/5)' in yellow when one or more streams are not in 'connected'
state. Normal emerald chip when everything is healthy.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 16 — Integration: partial-connect end-to-end

**Files:**
- Create: `tests/integration/health/test_partial_connect.py`

- [ ] **Step 1: Write the test**

```python
"""End-to-end: real SessionOrchestrator + FakeStream mix survives one
stream failing to connect, and incidents.jsonl captures it."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from syncfield.orchestrator import SessionOrchestrator
from syncfield.testing import FakeStream
from syncfield.types import SessionState


@pytest.mark.slow
def test_partial_connect_end_to_end(tmp_path: Path):
    sess = SessionOrchestrator(host_id="h", output_dir=tmp_path)
    sess.add(FakeStream("good_a"))
    sess.add(FakeStream("bad", fail_on_start=True))
    sess.add(FakeStream("good_b"))

    sess.connect()
    assert sess.state_name == SessionState.CONNECTED.value

    # Give the health worker a moment to ingest the startup-failure event.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if any(i.fingerprint == "bad:startup-failure" for i in sess.health.open_incidents()):
            break
        time.sleep(0.05)

    open_fps = [i.fingerprint for i in sess.health.open_incidents()]
    assert "bad:startup-failure" in open_fps

    sess.start(countdown_s=0)
    time.sleep(0.5)
    sess.stop()
    sess.disconnect()

    # incidents.jsonl should contain the startup-failure fingerprint.
    out = list(tmp_path.rglob("incidents.jsonl"))
    assert out, "no incidents.jsonl written"
    lines = [json.loads(l) for l in out[0].read_text().strip().splitlines() if l]
    fingerprints = {l["fingerprint"] for l in lines}
    assert "bad:startup-failure" in fingerprints
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/integration/health/test_partial_connect.py -v
```
Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/health/test_partial_connect.py
git commit -m "$(cat <<'EOF'
test(health): integration test for partial connect + incident persistence

Real SessionOrchestrator, mix of passing + failing FakeStreams; asserts
the session reaches CONNECTED, the failed stream surfaces as an open
startup-failure incident, and the fingerprint lands in incidents.jsonl.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 17 — Integration: NoDataDetector on a real orchestrator

**Files:**
- Create: `tests/integration/health/test_no_data_detector.py`

- [ ] **Step 1: Write the test**

```python
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
        super().__init__(stream_id=stream_id, kind="sensor", capabilities=StreamCapabilities())
        self._stop = threading.Event()
        self._gate = threading.Event()     # held closed until tests allow flow
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

    # Shrink threshold for the test via direct detector access.
    for d in sess.health.iter_detectors():
        if d.name == "no-data":
            d._threshold_ns = int(1e9)   # 1s
            break

    sess.connect()

    # After 1.5s, no-data incident should be open.
    time.sleep(1.5)
    open_fps = [i.fingerprint for i in sess.health.open_incidents()]
    assert "cam:no-data" in open_fps

    # Let samples flow → incident closes within ~1 tick.
    stream.allow_samples()
    time.sleep(0.5)

    sess.stop()
    sess.disconnect()

    resolved_fps = [i.fingerprint for i in sess.health.resolved_incidents()]
    assert "cam:no-data" in resolved_fps
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/integration/health/test_no_data_detector.py -v
```
Expected: 1 passed (~2s wall clock).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/health/test_no_data_detector.py
git commit -m "$(cat <<'EOF'
test(health): integration test for NoDataDetector on real orchestrator

SilentFakeStream connects but withholds samples until the test opens
its gate. Asserts the no-data incident opens within 1.5s of connect
and closes within one tick of samples resuming.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 18 — Final regression sweep + push

- [ ] **Step 1: Run the full suite**

```bash
uv run pytest tests/unit tests/integration/health -q --timeout 60
```
Expected: ≥870 passing (baseline before these tasks was 867; this plan adds ~6 new tests).

- [ ] **Step 2: Typecheck + build frontend**

```bash
cd src/syncfield/viewer/frontend && npx tsc --noEmit && npm run build
```
Expected: clean.

- [ ] **Step 3: Push**

```bash
git push
```

- [ ] **Step 4: Verify CI re-runs against the updated branch**

```bash
gh pr checks 20
```

Expected: CI kicks off; same pre-existing failures as before (3 Insta360 + audio + meta_quest tests) — this plan does not affect them.

---

## Self-Review Checklist

**Spec coverage (every spec requirement traces to a task):**

- Partial connect semantics (§ Goals 1) → Task 7
- Structured startup-failure event emission (§ Goals 2) → Task 7 (error path + success path)
- Per-stream ConnectionState + error in orchestrator (§ Data model) → Tasks 6, 8
- `StreamSnapshot.connection_state` / `connection_error` (§ Data model) → Task 9
- NoDataDetector (§ New detector) → Tasks 4, 5
- `Detector.observe_connection_state` hook (§ Architecture) → Task 1
- HealthWorker ingress queue (§ Architecture) → Task 2
- HealthSystem passthrough (§ Architecture) → Task 3
- Poller reads orchestrator state (§ Viewer changes) → Task 10
- Server WS serialization (§ Viewer changes) → Task 11
- Frontend ConnectionState + fields (§ Viewer changes) → Task 12
- Overlay components (§ Viewer changes) → Task 13
- StreamCard branch selection (§ Viewer changes) → Task 14
- Header degraded chip (§ Viewer changes) → Task 15
- Unit tests (§ Testing strategy) → Tasks 4, 7, 8, 9, 10, 11
- Integration tests (§ Testing strategy) → Tasks 16, 17

**Type consistency:** `ConnectionState` strings `"idle" | "connecting" | "connected" | "failed" | "disconnected"` — same in Python, same in TypeScript, same in every test assertion. Fingerprints `{stream_id}:startup-failure` and `{stream_id}:startup-success` consistent across orchestrator + detector + tests + spec.

**No placeholders:** every step shows a concrete code block or command.
