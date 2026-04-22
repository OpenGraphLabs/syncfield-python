# Partial Connect UX — Design Spec

- **Date**: 2026-04-22
- **Status**: Approved for implementation planning
- **Owner**: syncfield-python
- **Related spec**: `docs/superpowers/specs/2026-04-22-health-telemetry-design.md` (this extends it)

## Summary

Remove the "all-or-nothing" failure mode of `SessionOrchestrator.connect()`. When one stream's `connect()` raises (common case: an OAK camera with a specific `device_id` isn't plugged in), continue connecting the rest, mark the failed stream, and surface the failure in the viewer as a first-class visual state. Also detect the complementary case where a stream connects successfully but never emits a single sample (the observed OAK "black square" symptom), so the user is not left guessing whether the camera is alive. Together these changes close the silent-failure hole that remains even with the Phase-1 health-telemetry framework in place.

## Goals

1. **Partial connect resilience**: if any subset of streams fails to connect, the rest still transition to `CONNECTED` and recording can proceed with the survivors. The session only aborts when *every* stream fails.
2. **Structured startup failure emission**: each `stream.connect()` exception produces a `HealthEvent` shaped for `StartupFailureDetector` (`data["phase"]="connect"`, `data["outcome"]="error"`). Successes emit the matching success signal so the detector closes its incident if a stream recovers on a later connect.
3. **Per-stream connection state** visible to the viewer (`idle | connecting | connected | failed | disconnected`) plus a `connection_error` message for the failed case.
4. **Viewer state-aware stream card**: distinct visual for connecting / waiting-for-first-frame / failed, replacing the current grey "broken image" placeholder. Header chip reflects degraded state as `Ready (3/5)`.
5. **No-data detection**: a new detector fires when a stream has been in `connected` state for ≥ 5 s without emitting any sample. Closes the moment the first sample arrives.

## Non-goals

- **Automatic retry / reconnect policy**. A failed stream stays failed until the user explicitly triggers Disconnect → Connect again. Auto-retry is follow-up work.
- **Retry button on the failed card**. Same reason. The existing Disconnect/Connect flow already gets the user there in two clicks; adding a per-card retry button requires partial-reconnect semantics we are not designing yet.
- **Error category taxonomy / normalized error codes**. We show the exception message verbatim in the `connection_error` field for v1. Mapping "device not visible" vs "USB permission denied" vs "XLink handshake timeout" into structured categories is future work.
- **Crash-dump viewer integration**. Crash-dump paths remain surfaced as `IncidentArtifact` entries (delivered in Phase-1 OAK work). A "click to open" handler in the viewer is not in scope here.
- **Recovery UI for mid-recording failures**. This spec targets the connect phase. Detectors already cover mid-recording stalls / device crashes via Phase-1 work; they do not change here.

## Current state (what exists before this change)

- `SessionOrchestrator.connect()` iterates streams sequentially and calls `_rollback_disconnect_streams(connected)` on the first exception, re-raising it to the caller (`src/syncfield/orchestrator.py:1623-1643`). One failure collapses the whole session back to `IDLE`.
- On successful connect, the orchestrator emits a `HealthEvent(kind=HEARTBEAT, detail="connected")` but with no `phase` / `outcome` fields — so `StartupFailureDetector` never closes anything.
- On failure, the orchestrator emits **nothing** before re-raising — `StartupFailureDetector` never sees the error, so it is permanently dormant despite being registered.
- `StreamSnapshot` has no per-stream connection state. Frontend conflates "connecting", "connected but no data", and "failed" into one grey dot.
- `VideoPreview` renders the browser broken-image icon when `/stream/video/{id}` returns empty — no text, no visual indication of failure.
- OAK's `connect()` takes ~2.4 s (three enumeration retries × 0.8 s) before raising when its declared `device_id` is not present (`src/syncfield/adapters/oak_camera.py:316-366`).

## Architecture Overview

```
                       ┌──────────────────────────────────────────┐
                       │  SessionOrchestrator.connect()           │
                       │                                          │
                       │  for stream in self._streams.values():   │
                       │    _set_stream_state(sid, "connecting")  │
                       │    try:                                  │
                       │      stream.prepare()                    │
                       │      stream.connect()                    │
                       │    except Exception as exc:              │
                       │      _set_stream_state(sid, "failed")    │
                       │      _stream_errors[sid] = str(exc)      │
                       │      emit phase=connect outcome=error    │
                       │      continue                            │
                       │    _set_stream_state(sid, "connected")   │
                       │    emit phase=connect outcome=success    │
                       │                                          │
                       │  if no "connected" streams:              │
                       │    raise RuntimeError + IDLE             │
                       │  else: transition → CONNECTED            │
                       └────────────────────┬─────────────────────┘
                                            │
                       _set_stream_state  ──┼──► health.observe_connection_state(sid, new_state)
                                            │
                       ┌────────────────────▼─────────────────────┐
                       │  HealthSystem / HealthWorker             │
                       │                                          │
                       │   ingress: _ConnectionStateMsg (NEW)     │
                       │     → detector.observe_connection_state  │
                       │                                          │
                       │   existing path:                         │
                       │   on_sample  → detector.observe_sample   │
                       │   on_health  → tracker.ingest +          │
                       │                detector.observe_health   │
                       │                                          │
                       │   NoDataDetector tick:                   │
                       │     if connected_at[sid] + 5s ≤ now      │
                       │     and sid not in _has_sample:          │
                       │       emit {sid}:no-data incident        │
                       └────────────────────┬─────────────────────┘
                                            │
                       ┌────────────────────▼─────────────────────┐
                       │  SessionSnapshot (viewer wire)           │
                       │                                          │
                       │  streams[sid].connection_state (NEW)     │
                       │  streams[sid].connection_error (NEW)     │
                       │  active_incidents / resolved_incidents   │
                       │  (unchanged — already carry startup +    │
                       │   no-data incidents)                     │
                       └────────────────────┬─────────────────────┘
                                            │
                       ┌────────────────────▼─────────────────────┐
                       │  Viewer frontend                         │
                       │                                          │
                       │  StreamCard selects body by state:       │
                       │    "connecting" → ConnectingOverlay      │
                       │    "failed"     → FailedOverlay + error  │
                       │    "connected"  + frame_count === 0      │
                       │                 → WaitingForDataOverlay  │
                       │    else         → <VideoPreview/>        │
                       │                                          │
                       │  Header state chip:                      │
                       │    "Ready (3/5)" if connected < total    │
                       │    "Ready"       if all connected        │
                       │  IncidentPanel + severity badge: unchanged│
                       └──────────────────────────────────────────┘
```

## Data model

### `ConnectionState` (new)

```python
ConnectionState = Literal[
    "idle",           # stream added but connect() not yet called
    "connecting",     # connect() is running
    "connected",      # connect() returned successfully
    "failed",         # connect() raised; error recorded in _stream_errors
    "disconnected",   # disconnect() ran, or rolled back after global failure
]
```

Tracked on `SessionOrchestrator` as `self._stream_states: dict[str, ConnectionState]` and `self._stream_errors: dict[str, str]` (populated only for `failed`). Cleared on `disconnect()` back to `disconnected`.

Transitions are a narrow DAG:
```
idle → connecting → connected → disconnected
                  → failed    → disconnected
```
`failed` never transitions to `connected` within the same connect cycle — a subsequent `disconnect()` followed by `connect()` is a new cycle.

### `StreamSnapshot` — additions

```python
connection_state: str            # one of ConnectionState literals
connection_error: str | None     # set iff connection_state == "failed"
```

### `StartupFailureDetector` event contract (no code change; existing design)

Orchestrator emits on connect failure — using the detector's own fingerprint so both the orchestrator's synchronous event and the detector's tick-emitted event land on the same incident:
```python
HealthEvent(
    stream_id=stream.id,
    kind=HealthEventKind.ERROR,
    at_ns=time.monotonic_ns(),
    detail=str(exc),
    severity=Severity.ERROR,
    source="orchestrator",
    fingerprint=f"{stream.id}:startup-failure",
    data={"phase": "connect", "outcome": "error", "error": str(exc)},
)
```

On success:
```python
HealthEvent(
    stream_id=stream.id,
    kind=HealthEventKind.HEARTBEAT,
    at_ns=time.monotonic_ns(),
    detail="connected",
    severity=Severity.INFO,
    source="orchestrator",
    fingerprint=f"{stream.id}:startup-success",
    data={"phase": "connect", "outcome": "success"},
)
```

Rationale for fingerprints:
- `{stream.id}:startup-failure` routes to `StartupFailureDetector` via `IncidentTracker._detector_for` (fingerprint middle token equals the detector's `name`). The detector owns `close_condition` which consults its own `_recovered` set, so the incident closes only after a matching success signal arrives.
- The success event deliberately uses a **different** fingerprint (`:startup-success`), so it does not itself open an incident on the failure one. Its purpose is purely to feed `StartupFailureDetector.observe_health`, which keys off `data["outcome"] == "success"` and adds the stream to `_recovered`. The event still flows through the tracker, but the tracker's per-fingerprint grouping means `:startup-success` events open at most a trivial INFO incident that auto-closes via the passthrough quiet window (no user-visible noise given severity=INFO).

## New detector: `NoDataDetector`

Lives at `src/syncfield/health/detectors/no_data.py`. Registered by default in `HealthSystem._install_default_detectors`.

- Fires `HealthEvent(fingerprint=f"{stream_id}:no-data", severity=ERROR)` when a stream has been in `connected` state for ≥ `threshold_ns` (default 5 s) without any sample.
- Closes the incident the moment the first sample arrives (`close_condition` returns `stream_id in self._has_sample`).
- Per-stream state machine keyed by `stream_id`: `_connected_at`, `_has_sample`, `_fire_active`. Reset to empty on `observe_connection_state(stream_id, new_state)` whenever the new state is `failed / disconnected / idle`.

### `Detector.observe_connection_state` (new hook)

Added to the protocol and to `DetectorBase` as a no-op. Existing detectors keep working unchanged. `NoDataDetector` is the only one that overrides it for v1.

### Worker ingress

`HealthWorker` gains a fifth queue:
```python
_connection_states: queue.SimpleQueue[_ConnectionStateMsg]
```
drained on each tick the same way the other four are. `HealthSystem.observe_connection_state(stream_id, new_state)` pushes into it. Called by `SessionOrchestrator._set_stream_state`.

## Viewer changes

### Backend (`viewer/state.py`, `viewer/poller.py`, `viewer/server.py`)

- `StreamSnapshot`: add `connection_state: str = "idle"` and `connection_error: str | None = None` fields.
- Poller reads `orchestrator._stream_states` and `orchestrator._stream_errors` on each tick and populates the snapshot.
- Server WebSocket serializer includes both fields in each stream entry.

### Frontend types (`lib/types.ts`)

```ts
export type ConnectionState =
  | "idle" | "connecting" | "connected" | "failed" | "disconnected";

export interface StreamSnapshot {
  // ...existing fields...
  connection_state: ConnectionState;
  connection_error: string | null;
}
```

### `StreamCard` body selection

```tsx
function StreamCardBody({ stream }: { stream: StreamSnapshot }) {
  if (stream.connection_state === "connecting") return <ConnectingOverlay />;
  if (stream.connection_state === "failed") {
    return <FailedOverlay error={stream.connection_error ?? "Unknown error"} />;
  }
  if (stream.connection_state === "connected" && stream.frame_count === 0) {
    return <WaitingForDataOverlay />;
  }
  return <VideoPreview streamId={stream.id} />;
}
```

### New overlay components (`components/stream-overlays.tsx`)

- `ConnectingOverlay` — neutral grey background, pulse-animated dot + "Connecting…" text.
- `WaitingForDataOverlay` — soft yellow tint, "Connected · waiting for first frame". No animation (it should feel temporary; the real alarm is the no-data incident that opens after 5 s).
- `FailedOverlay` — red background, "Failed to connect" header, monospace error text (two visible lines, clickable to expand to full text), hint "Press Discover Devices or Disconnect + Connect to retry".

### Header state chip (`components/header.tsx`)

```tsx
const total = Object.keys(snapshot.streams).length;
const connected = Object.values(snapshot.streams)
  .filter((s) => s.connection_state === "connected").length;
const label = connected === total || total === 0
  ? stateLabel
  : `${stateLabel} (${connected}/${total})`;
const tone = connected < total ? "warning" : "normal";
```
Warning-tone chip is yellow; normal chip keeps its current styling.

## Orchestrator integration points

All changes in `src/syncfield/orchestrator.py`:

1. **`__init__`**: initialize `self._stream_states: dict[str, ConnectionState] = {}` and `self._stream_errors: dict[str, str] = {}`.
2. **`add(stream)`**: after existing wiring, set `self._stream_states[stream.id] = "idle"`.
3. **`connect()`**: replace the current all-or-nothing loop (lines 1623-1643) with the partial-connect loop in the Architecture Overview. Use a single helper `_set_stream_state(stream_id, new_state)` that updates `_stream_states` *and* calls `self.health.observe_connection_state(stream_id, new_state)`.
4. **`disconnect()`**: only call `stream.disconnect()` on streams whose `_stream_states[sid]` is `connected` (or was up to `stop_recording`). Transition failed streams directly to `disconnected`. Clear `_stream_errors`.
5. **`stop()`**: unchanged by this spec — it already handles only streams that were recording. Failed streams never entered the recording set.

Remove the now-dead `_rollback_disconnect_streams` helper if nothing else uses it, otherwise leave it untouched.

## Persistence & artifacts

No new files. The startup-failure and no-data incidents flow through the existing `incidents.jsonl` sidecar via the callbacks wired in Phase 1.

## Testing strategy

### Unit tests

**`tests/unit/health/detectors/test_no_data.py`** — new file. Covers:
- No fire when `connected` for less than `threshold_ns`.
- Fires after threshold with no sample.
- Closes after first sample arrives.
- Does not re-fire for the same stream while still in the connected-no-data state (uses `_fire_active`).
- Resets bookkeeping on state transition back to `failed` / `disconnected` / `idle`.
- Per-stream independent state.

**`tests/unit/health/test_health_worker.py`** (extend) — verify the worker drains a `_connection_states` queue and fans out to `observe_connection_state` on every registered detector.

**`tests/unit/health/test_detector_base.py`** (extend) — confirm the new `observe_connection_state` hook is a safe no-op on the base class.

**`tests/unit/test_orchestrator.py`** (new test class `TestPartialConnect`) — the orchestrator tests already use `FakeStream` with `fail_on_start` / `fail_on_connect` flags. Cover:
- One stream raising in connect: session ends up in `CONNECTED`; that stream's `connection_state == "failed"`; others are `"connected"`.
- All streams raising: session returns to `IDLE` and the aggregate error is raised.
- `_stream_errors[sid]` contains the exception message string.
- `StartupFailureDetector` sees a `phase="connect"` event (spy detector).
- `disconnect()` does not call `stream.disconnect()` on streams whose state is `failed`.

### Integration tests

**`tests/integration/health/test_partial_connect.py`** — new file. Spins a real `SessionOrchestrator` with one `FakeStream` that always raises in `connect()` and two normal `FakeStream`s. Drives the full connect → record → stop → disconnect lifecycle and asserts:
- Session reaches `CONNECTED` (not `IDLE`).
- The startup-failure incident is in `sess.health.open_incidents()` after connect.
- `incidents.jsonl` contains the fingerprint.
- Working streams produce samples; failed stream does not.

**`tests/integration/health/test_no_data_detector.py`** — a `FakeStream` whose `connect()` returns but whose sample-emission thread is inhibited. After 6 s of wall-clock, the no-data incident is open. Resuming sample emission closes the incident within one tick window.

### Frontend

Component test for `StreamCard` branch selection: one test per state (`connecting` / `failed` / `connected-no-frames` / `connected-with-frames`). Assert the correct overlay component is rendered. The overlays themselves get a trivial render test each.

Header chip rendering: three cases (`total === 0`, `connected === total`, `connected < total`).

## Scope for this PR (`feat/health-telemetry`)

Adds to the existing PR on top of the Phase 1 health-telemetry work. Ordered by dependency:

1. **Data model**: `ConnectionState` literal type, `StreamSnapshot.connection_state` / `connection_error` fields.
2. **`Detector.observe_connection_state` hook** on protocol + base.
3. **`HealthWorker` new ingress queue**; `HealthSystem.observe_connection_state` passthrough.
4. **`NoDataDetector`** + default-register in `HealthSystem`.
5. **`SessionOrchestrator.connect()` partial-connect rewrite** + `_set_stream_state` helper + structured startup events.
6. **Poller + server** snapshot serialization.
7. **Frontend overlays + card branch + header chip**.
8. **All tests listed above**.

## Open questions / deferred decisions

- Exact `threshold_ns` for `NoDataDetector` — proposed 5 s. Short enough to catch a silent OAK within the window a user is watching; long enough to absorb the natural warm-up of cameras that take a second or two to produce the first frame after connect.
- `FailedOverlay` copy — current draft is "Failed to connect" + verbatim error + "Press Discover Devices or Disconnect + Connect to retry". Final copy is a UX polish pass, not a design gate.

## Risks

- **Orchestrator `disconnect()` paths** need a careful read. If any bookkeeping currently assumes "every stream the orchestrator knows is fully connected", it may drop frames or mis-close files for a stream that was `failed` and never entered recording. The partial-connect rewrite must explicitly skip `failed` streams everywhere a loop iterates `self._streams.values()`.
- **Severity=INFO incident from success events** — the success signal uses `{stream_id}:startup-success` and creates a short-lived INFO incident that auto-closes via the passthrough quiet window. At `severity=INFO` it does not appear in the IncidentPanel's Active Issues (which filters by severity visually in the existing UI), but it does land in `incidents.jsonl`. If that persisted-log noise is unwanted, we can suppress the tracker's ingest for events whose fingerprint ends with `:startup-success`. Deferred until we see real post-session logs and decide whether it's useful telemetry or noise.
- **Frontend overlay stacking with `severity badge`** — the per-stream badge (Phase 1) paints on the card header; the overlays paint on the body. They should not collide, but the implementation must verify the card layout renders sensibly in all five states.
