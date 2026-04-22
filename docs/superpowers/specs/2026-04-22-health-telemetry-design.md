# Health Telemetry Platform — Design Spec

- **Date**: 2026-04-22
- **Status**: Approved for implementation planning
- **Owner**: syncfield-python
- **Related**: `src/syncfield/orchestrator.py` (session lifecycle), `src/syncfield/stream.py` (Stream protocol, existing `HealthEvent`), `src/syncfield/adapters/oak_camera.py` (motivating failure modes), `src/syncfield/viewer/` (live + post-session surface)

## Summary

Add a sensor-agnostic, platform-level **health telemetry system** to syncfield. During a recording the system continuously observes every active stream (sample cadence, silence gaps, adapter-reported faults, writer backpressure) and raises **Incidents** — Sentry-style grouped objects with severity, open/close lifecycle, and attached artifacts — that surface in the viewer in real time and are persisted for post-session review. New detectors can be added without touching any adapter; new adapters inherit the full baseline check suite for free.

The first motivating hardware is OAK (Luxonis DepthAI), where native log output (`X_LINK_ERROR`, `Device has crashed`, crash-dump paths, `Reconnection successful`) today vanishes into stderr. The design bridges that native logger into the unified telemetry channel, and the same detector set catches identical symptoms (stall, FPS drop) on any future adapter.

## Goals

1. **Live detection, live display**: while recording, surface hardware crashes, stream stalls, FPS drops, jitter spikes, startup failures, writer backpressure, and adapter-reported faults within ~1s of occurrence. Render them as a first-class **Active Issues** panel in the viewer.
2. **Post-session incident report**: persist a structured `incidents.jsonl` per session and expose `FinalizationReport.incidents`, so users can answer "what went wrong, when, on which stream, with what evidence?" after the fact.
3. **Sensor-agnostic baseline**: every stream — current and future — automatically gets stall detection, FPS-drop detection, jitter detection, startup-failure detection, and adapter-event pass-through, with zero adapter code changes.
4. **Pluggable detectors**: adding a new detection rule (temperature, bandwidth, battery, anomaly) is a single-file, single-class addition registered at startup. No adapter modifications.
5. **Default-on**: users never need to wire up health telemetry; a freshly constructed `SessionOrchestrator` has the full baseline running.
6. **Artifact capture**: when a device provides crash evidence (e.g., OAK's `crash_dump.json`), attach the path to the incident so the user can ship it to the vendor.
7. **Zero impact on capture hot path**: detectors observe via lock-free hand-off; all detection work runs on a dedicated worker thread.

## Non-goals

- **Multihost fan-in (leader-side fleet view)**: follower incidents are written to the follower's session log and collected post-stop via the existing file-pull path. Real-time follower→leader streaming of health events is deferred.
- **Cross-session history / search**: no database, no aggregation across sessions. Each session is self-contained.
- **Automatic remediation**: the platform detects and reports. It does not auto-reconnect devices, swap to backup streams, or halt recording on failure.
- **Frame-content anomaly detection**: no checks for black frames, codec corruption, audio silence content, or ML-based quality scoring.
- **Host resource monitoring**: CPU/GPU/memory/thermal of the host machine are out of scope.
- **Cross-session markdown summary**: `incidents.jsonl` is structured; a human-readable post-mortem generator is deferred.
- **Backward compatibility of existing health surfaces**: `StreamSnapshot.health_count` / `problem_count` are removed in favor of the new incident-based fields. Existing `session_log.jsonl` consumers must migrate. There are no external consumers today.

## Current state (what exists)

- `HealthEvent` dataclass with `HealthEventKind = {HEARTBEAT, DROP, RECONNECT, WARNING, ERROR}` in `src/syncfield/types.py:250-273`.
- `StreamBase._emit_health(event)` for adapters to push into the session log — `src/syncfield/stream.py:237-241`.
- `SessionLogWriter.log_health(event)` persists to `session_log.jsonl`.
- `SessionOrchestrator._on_stream_health()` routes events to log + buffered collection — `src/syncfield/orchestrator.py:2354-2363`.
- Viewer `HealthTable` component renders last 20 raw events as a timeline.
- `FinalizationReport.health_events[]` includes raw events post-stop.

**What is missing, and what this spec adds**: no automatic detection (all events are adapter-emitted); no severity; no grouping; no open/close semantics; no detector plugin model; no artifact attachment; no Sentry-style UI surface; no per-adapter target-hz hint for comparing observed vs. expected cadence.

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│ SessionOrchestrator                                                      │
│                                                                          │
│  ┌──────────┐   on_sample   ┌──────────────────────────┐                │
│  │ Streams  │──────────────►│                          │                │
│  └──────────┘   on_health   │                          │                │
│        │                    │      HealthSystem        │                │
│        │  state changes ───►│                          │                │
│        │                    │  ┌────────────────────┐  │                │
│        ▼                    │  │ DetectorRegistry   │  │                │
│  ┌──────────┐  queue stats  │  │  (default + user)  │  │                │
│  │  Writer  │──────────────►│  └────────────────────┘  │                │
│  └──────────┘               │           │              │                │
│                             │           ▼              │                │
│                             │  ┌────────────────────┐  │                │
│                             │  │  HealthWorker      │  │  ◄── 20 Hz    │
│                             │  │  (thread, ticks)   │  │      tick     │
│                             │  └────────────────────┘  │                │
│                             │           │              │                │
│                             │           ▼              │                │
│                             │  ┌────────────────────┐  │                │
│                             │  │  IncidentTracker   │  │                │
│                             │  │  open / close /    │  │                │
│                             │  │  fingerprint group │  │                │
│                             │  └────────────────────┘  │                │
│                             │           │              │                │
│                             └───────────┼──────────────┘                │
│                                         │                               │
│                ┌────────────────────────┼────────────────────────┐     │
│                │                        │                        │     │
│                ▼                        ▼                        ▼     │
│        session_log.jsonl         incidents.jsonl         SessionSnapshot
│        (raw HealthEvents)        (Incident records)      → WebSocket   │
│                                         │                              │
└─────────────────────────────────────────┼──────────────────────────────┘
                                          ▼
                              FinalizationReport.incidents
                                (exposed from SDK)
```

**Threading model**: streams continue to call `on_sample` / `_emit_health` from capture threads. These fan into lock-free deques drained by a single `HealthWorker` daemon thread (20 Hz). All detector ticks and incident-tracker state live on that thread — there are no locks on the capture hot path beyond the deque push.

## Data model

### Severity

```python
class Severity(str, Enum):
    INFO     = "info"       # heartbeat, reconnect-success, config warning
    WARNING  = "warning"    # mild FPS dip, jitter, macOS deprecation warning
    ERROR    = "error"      # disconnect, X_LINK_ERROR, encoding fail, silence > 2s
    CRITICAL = "critical"   # device crash, multi-stream simultaneous down
```

### HealthEvent (enriched, existing class; new fields)

```python
@dataclass(frozen=True)
class HealthEvent:
    stream_id: str
    kind: HealthEventKind          # existing
    at_ns: int                     # existing, time.monotonic_ns()
    detail: str | None             # existing

    # new fields (no backward compat shim — session_log.jsonl format changes):
    severity: Severity
    source: str                    # "adapter:oak" | "detector:stream-stall" | ...
    fingerprint: str               # stable grouping key, e.g. "oak-main:stream-stall"
    data: dict[str, Any] = {}      # structured context: {"observed_hz": 12, "target_hz": 30}
```

The `fingerprint` is what groups many raw events into one Incident. Detectors own the fingerprint formula (typically `f"{stream_id}:{detector.name}"`, optionally refined for sub-categorization). Adapter-emitted events use `f"{stream_id}:adapter:{kind.value}"` via the pass-through detector.

### Incident (new)

```python
@dataclass
class Incident:
    id: str                           # ulid, stable for the lifetime of the session
    stream_id: str
    fingerprint: str                  # same as the events it groups
    title: str                        # human-readable, e.g., "Stream stalled (silence 9s)"
    severity: Severity                # = max severity across grouped events
    source: str                       # name of the first detector/adapter that fired

    opened_at_ns: int
    closed_at_ns: int | None          # None = still open / active
    last_event_at_ns: int             # most recent event in the group

    event_count: int
    first_event: HealthEvent          # snapshot for display
    last_event:  HealthEvent          # snapshot for display

    artifacts: list[IncidentArtifact] = []   # attached evidence
    data: dict[str, Any] = {}                # aggregated context (e.g., min_observed_hz)

@dataclass(frozen=True)
class IncidentArtifact:
    kind: str        # "crash_dump" | "log_excerpt" | ...
    path: str        # absolute path, or URI
    detail: str | None = None
```

An incident is **open** while its condition persists. The tracker closes it when the owning detector's `close_condition` returns True (for detector-owned incidents) or when a quiet period elapses without a new matching event (for pass-through adapter incidents; default 30s).

## Detector system

### Detector protocol

```python
class Detector(Protocol):
    name: str                             # "stream-stall", "fps-drop", ...
    default_severity: Severity            # severity of events this detector raises

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None: ...
    def observe_health(self, stream_id: str, event: HealthEvent) -> None: ...
    def observe_state(self, old: SessionState, new: SessionState) -> None: ...
    def observe_writer_stats(self, stream_id: str, stats: WriterStats) -> None: ...

    def tick(self, now_ns: int) -> Iterable[HealthEvent]: ...
    def close_condition(self, incident: Incident, now_ns: int) -> bool: ...
```

- `observe_*` feed the detector its raw signals; they must be fast and non-blocking.
- `tick` is called by the `HealthWorker` at 20 Hz; this is where silence/gap-based detections fire.
- `close_condition` decides when an open incident with this detector's fingerprint can be closed.

A `DetectorBase` ABC provides no-op defaults for every hook so subclasses only implement what they need.

### Default detector suite (registered automatically on `SessionOrchestrator` construction)

| Detector | Signal | Fires when | Closes when |
|---|---|---|---|
| `StreamStallDetector` | `observe_sample` (timestamps) + `tick` | no sample for 2.0s on a stream that previously had samples | sample resumes and keeps flowing 1.0s |
| `FpsDropDetector` | `observe_sample` + `tick` | effective FPS < 70% of `target_hz` for 3.0s (or, without `target_hz`, < 70% of learned 10s-baseline after a 5s warmup) | FPS ≥ 90% for 5.0s |
| `JitterDetector` | `observe_sample` + `tick` | p95 inter-sample gap over last 60 samples > 2× expected | p95 ≤ 1.2× expected for 10.0s |
| `StartupFailureDetector` | `observe_health` (for adapter-raised startup errors) + `observe_state` | `connect()` or `start_recording()` raises / times out | subsequent retry reports success |
| `BackpressureDetector` | `observe_writer_stats` | writer queue depth > 80% of capacity for 2.0s, OR writer drop counter increments | queue depth < 30% for 5.0s |
| `AdapterEventPassthrough` | `observe_health` | any adapter-emitted `HealthEvent` not already owned by another detector | 30s quiet on the fingerprint |
| `DepthAILoggerBridge` | native depthai Python logger handler | depthai error/warning log arrives (X_LINK_ERROR, "Device has crashed", reconnect events) → converted to `HealthEvent` → fed into `AdapterEventPassthrough` | (stateless — it is a translator, not a detector) |

`DepthAILoggerBridge` is not technically a detector in the "fire from tick" sense — it is a logging-handler adapter. For symmetry it lives next to detectors and registers via the same registry, but it only produces `HealthEvent`s; `AdapterEventPassthrough` groups them into incidents. The bridge is installed **automatically** when the `oak` optional extra is present (detected by importability of `depthai`), matching the project's default-on philosophy.

### DetectorRegistry

```python
class DetectorRegistry:
    def register(self, detector: Detector) -> None: ...
    def unregister(self, name: str) -> None: ...
    def __iter__(self) -> Iterator[Detector]: ...
```

Users register additional detectors before `session.connect()`:

```python
session = SessionOrchestrator(...)
session.health.register(MyTemperatureDetector(...))
```

Default detectors are installed by `HealthSystem` in `__init__`; users can `unregister` them by name to opt out.

### HealthWorker & IncidentTracker

- `HealthWorker` is a daemon thread that owns the tick loop and all mutable state. It exposes thread-safe ingress queues (`Queue` or lock-free deque) for samples, health events, state transitions, and writer stats.
- `IncidentTracker` (running on the worker thread) consumes every `HealthEvent` produced by detectors or observed via pass-through, groups by fingerprint, opens new incidents, updates existing ones, and runs `close_condition` each tick for every open incident.
- Closed incidents stay in the tracker's memory (for the viewer's "Resolved this session" list) and are flushed to `incidents.jsonl` on each state change and on `stop()`.

## Integration points

### `src/syncfield/types.py`
- Add `Severity` enum.
- Enrich `HealthEvent` with `severity`, `source`, `fingerprint`, `data`.
- Add `Incident`, `IncidentArtifact`, `IncidentSnapshot` (read-only view used in WebSocket payloads).
- Add `WriterStats` dataclass (queue depth, drop count, bytes flushed).
- `FinalizationReport` gains `incidents: list[Incident]`. `health_events` field remains.

### `src/syncfield/stream.py`
- `StreamCapabilities` gains `target_hz: float | None = None`.
- No API change to `StreamBase._emit_health`; it continues to push `HealthEvent` and now the system assigns `severity`/`fingerprint`/`source` server-side if the adapter did not.

### `src/syncfield/health/` (new package)
```
src/syncfield/health/
├── __init__.py              # public: HealthSystem, Severity, Incident, Detector, DetectorBase
├── types.py                 # Incident, IncidentArtifact, IncidentSnapshot, WriterStats
├── severity.py              # Severity enum + helpers
├── system.py                # HealthSystem (user-facing facade, owns worker + registry + tracker)
├── worker.py                # HealthWorker (thread, tick loop, ingress queues)
├── registry.py              # DetectorRegistry
├── tracker.py               # IncidentTracker
├── detector.py              # Detector protocol + DetectorBase
└── detectors/
    ├── __init__.py
    ├── stream_stall.py
    ├── fps_drop.py
    ├── jitter.py
    ├── startup_failure.py
    ├── backpressure.py
    ├── adapter_passthrough.py
    └── depthai_bridge.py     # soft-imports depthai; registered if importable
```

### `src/syncfield/orchestrator.py`
- `SessionOrchestrator.__init__` constructs `self.health = HealthSystem(session_id=..., clock=...)`.
- On adding a stream: subscribe `health.observe_sample` and `health.observe_health` to that stream.
- On every `SessionState` transition: `health.observe_state(old, new)`.
- On each writer flush: pump `WriterStats` into `health.observe_writer_stats`.
- `start()` boots the worker thread; `stop()` drains, closes still-open incidents with `closed_at_ns = now`, and embeds `incidents` into `FinalizationReport`.
- Existing `_on_stream_health` is simplified — it now just forwards to `health`. `session_log.jsonl` writing is owned by `SessionLogWriter`, which also gains `log_incident(incident)`.

### `src/syncfield/writer.py`
- `SessionLogWriter` gains `log_incident(incident)` writing to `incidents.jsonl`.
- On every incident open/update/close, `IncidentTracker` calls `writer.log_incident(...)`.
- The writer also emits `WriterStats` snapshots at a low frequency (e.g., every 250ms) into `HealthSystem` for backpressure detection.

### `src/syncfield/adapters/oak_camera.py`
- Declare `target_hz` in `StreamCapabilities`.
- At `connect()`: attach `DepthAILoggerBridge` to the depthai Python logger (scoped to this stream instance).
- At crash detection (depthai signals device crash or the bridge observes the "Crash dump logs are stored in" line): emit a `HealthEvent` with `kind=ERROR`, `severity=CRITICAL`, `data={"crash_dump_path": "..."}`. The tracker attaches it as an `IncidentArtifact(kind="crash_dump", path=...)`.
- No other OAK-specific detection code. `StreamStallDetector` and `FpsDropDetector` handle the "frozen for 9s during depthai reconnect" case generically.

### `src/syncfield/viewer/state.py` & `server.py`
- `SessionSnapshot` gains:
  - `active_incidents: list[IncidentSnapshot]`
  - `resolved_incidents: list[IncidentSnapshot]`  (capped at 20 most recent)
- `StreamSnapshot.health_count` and `StreamSnapshot.problem_count` **removed**; per-stream incident counts derived client-side by filtering `active_incidents` by `stream_id`.
- Poller subscribes to `HealthSystem` for incident opened/updated/closed notifications (via a simple callback protocol) and merges them into the snapshot.

### `src/syncfield/viewer/frontend/src/`
- Delete `components/health-table.tsx`.
- Add `components/incident-panel.tsx` (Option α):
  - Two collapsible sections: **Active Issues (N)**, **Resolved this session (N)**.
  - Each incident card: severity icon · stream id · title · opened/recovered relative time · event count · artifact chips.
  - Click to expand → raw `HealthEvent` list for that fingerprint pulled from `health_log` (retained in snapshot, capped at 200 most recent overall).
- Stream cards: replace red dot count with severity-colored badge (critical=red, error=orange, warning=yellow) whose number reflects the count of open incidents on that stream.
- Shared type file `lib/types.ts` mirrors `IncidentSnapshot` / `Severity`.

## OAK — concrete event → incident mapping

Reference sample from the user's session log:

```
[depthai] [error] Communication exception ... 'Couldn't read data from stream: '__x_0_1' (X_LINK_ERROR)'
[host] [warning] Closed connection
[host] [warning] Attempting to reconnect. Timeout is 10000ms
[depthai] [error] Device with id ... has crashed. Crash dump logs are stored in: /path/to/crash_dump.json
[host] [warning] Reconnection successful
```

Pipeline:

1. `DepthAILoggerBridge` converts each native line into a `HealthEvent`:
   - `X_LINK_ERROR` → `kind=ERROR`, `severity=ERROR`, `source="adapter:oak"`, `fingerprint="oak-main:adapter:xlink-error"`, `data={"stream": "__x_0_1"}`.
   - "Closed connection" → `kind=WARNING`, `severity=WARNING`, `fingerprint="oak-main:adapter:connection-closed"`.
   - "Attempting to reconnect" → `kind=RECONNECT`, `severity=INFO`, `fingerprint="oak-main:adapter:reconnect-attempt"`.
   - "Device has crashed" → `kind=ERROR`, `severity=CRITICAL`, `fingerprint="oak-main:adapter:device-crash"`, `data={"crash_dump_path": "/path/to/crash_dump.json"}`.
   - "Reconnection successful" → `kind=RECONNECT`, `severity=INFO`, `fingerprint="oak-main:adapter:reconnect-success"`.
2. `AdapterEventPassthrough` groups the four non-info fingerprints into incidents.
3. Concurrently, `StreamStallDetector` observes no sample for 2s and opens its own incident `fingerprint="oak-main:stream-stall"` with `severity=ERROR`.
4. When samples resume (bridge sees "Reconnection successful" and stream yields samples again), `StreamStallDetector.close_condition` returns True after 1s of steady flow → the stall incident closes.
5. The device-crash incident gains an `IncidentArtifact(kind="crash_dump", path=...)` attached from its event's `data`.
6. On `stop()`, all these incidents flush to `incidents.jsonl`.

The macOS `NSCameraUseContinuityCameraDeviceType` warning is emitted by the UVC adapter similarly (UVC adapter adds a tiny stderr-watcher or converts via existing `_emit_health`) as `severity=INFO`, `fingerprint="uvc-cam:adapter:continuity-deprecation"`; surfaces as a single resolved informational incident.

## Persistence & artifacts

Per-session directory additions:

```
session/<id>/
├── session_log.jsonl       # existing — raw HealthEvents (format updated, not backward-compat)
├── incidents.jsonl         # new — one Incident per line; appended on open/update/close
├── manifest.json           # existing — gains "incidents_count", "active_at_stop_count"
└── <stream_id>/...         # existing per-stream files; crash dumps referenced via absolute path
```

- `incidents.jsonl` is append-only. Each line is the current full state of the incident at write time; readers compact by `incident.id`.
- `FinalizationReport.incidents` is the in-memory compacted list (unique by `id`, latest state).

## Testing strategy

Every new module below `health/` ships with unit tests. The following are the load-bearing test suites:

**`tests/health/test_detectors.py`** — one class per detector. Uses a `FakeClock` and a synthetic sample stream generator. Each test asserts: (a) the detector fires on the trigger, (b) does not fire on noise, (c) `close_condition` returns True after recovery, (d) fingerprint is stable across runs.

**`tests/health/test_incident_tracker.py`** — grouping, opening, closing, reopening (same fingerprint after close), severity escalation (an open WARNING incident that receives an ERROR event upgrades), artifact attachment, JSONL flush on close.

**`tests/health/test_health_system_integration.py`** — end-to-end with a `FakeStream` (a `StreamBase` subclass that emits synthetic samples on a driven clock): (a) stall then recover → one incident opens and closes; (b) sustained FPS drop → one incident; (c) adapter emits `HealthEvent(kind=ERROR)` → pass-through creates incident; (d) crash event carrying `data["crash_dump_path"]` produces artifact.

**`tests/health/test_depthai_bridge.py`** — feeds synthetic depthai log records (as `logging.LogRecord`) and asserts the correct `HealthEvent`s come out. No real depthai device required.

**`tests/orchestrator/test_orchestrator_health_integration.py`** — real `SessionOrchestrator` with `FakeStream` registered; drives the full lifecycle and asserts `FinalizationReport.incidents` content and `incidents.jsonl` content match.

**`tests/viewer/test_snapshot_incidents.py`** — poller snapshot correctly includes `active_incidents` / `resolved_incidents`; stream filtering works.

Frontend: at least a component test for `IncidentPanel` rendering Active + Resolved sections and a click-to-expand interaction.

## Implementation phases (for the writing-plans step)

1. **Core types & skeleton** — `Severity`, enriched `HealthEvent`, `Incident`, `IncidentArtifact`, `WriterStats`. `health/` package skeleton with `HealthSystem`, `HealthWorker`, `DetectorRegistry`, `IncidentTracker`, `DetectorBase`. Unit tests for tracker & worker.
2. **Platform detectors** — `StreamStallDetector`, `FpsDropDetector`, `JitterDetector`, `StartupFailureDetector`, `BackpressureDetector`, `AdapterEventPassthrough`. Full unit-test coverage.
3. **Orchestrator & writer integration** — wire `SessionOrchestrator` to `HealthSystem`, remove `StreamSnapshot.health_count`/`problem_count`, add writer stats emission, `incidents.jsonl` flushing, `FinalizationReport.incidents`. Integration tests with `FakeStream`.
4. **OAK bridge & `target_hz`** — `DepthAILoggerBridge`, crash-dump artifact attachment, declare `target_hz` on `OakCameraStream`. Bridge unit tests.
5. **Viewer — server** — `IncidentSnapshot` in `SessionSnapshot`; poller ingests from `HealthSystem`; remove obsolete fields. Snapshot tests.
6. **Viewer — frontend** — new `incident-panel.tsx`, deleted `health-table.tsx`, stream-card badge update, types mirrored. Component tests.
7. **Per-adapter target_hz rollout** — add `target_hz` to every adapter that has a known target (`UVCWebcamStream`, `HostAudioStream`, `MetaQuestCameraStream`, `Go3SStream` where relevant, etc.). No other changes.
8. **Manual verification checklist** — reproduce the OAK crash sequence on a real rig; confirm incidents open, close, and artifact attaches; screenshot `IncidentPanel`.

## Open design questions (to resolve during planning)

None blocking. Low-stakes items to be resolved during implementation:

- Exact `target_hz`-learning window length when unset (currently proposed 5s warmup, 10s rolling baseline).
- Whether `DetectorRegistry.unregister` is public API for v1 or internal-only.
- Whether `IncidentSnapshot` includes the full `first_event` / `last_event`, or just their `detail` strings (wire size tradeoff).

## Risks

- **Noisy detectors**: a poorly tuned `JitterDetector` could fire constantly on cameras with naturally irregular cadence. Mitigation: per-adapter `jitter_tolerance` hint in `StreamCapabilities`, default conservative.
- **Depthai logger bridge fragility**: parses string patterns; a depthai version bump could change log format. Mitigation: pattern-match liberally and fall back to `severity=WARNING` with `source="adapter:oak:unparsed-log"` rather than dropping the line. Bridge has its own targeted tests.
- **Thread starvation**: if the worker tick blocks, detections are delayed. Mitigation: all detector `tick` / `observe_*` methods must be non-blocking; enforced by tests that time individual calls.
