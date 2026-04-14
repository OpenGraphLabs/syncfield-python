# Insta360 Go3S Adapter — Design Spec

- **Date**: 2026-04-14
- **Status**: Approved for implementation planning
- **Owner**: syncfield-python
- **Related**: `opengraph-studio/recorder` (source of BLE/WiFi logic), `syncfield-swift` (reference Swift implementation)

## Summary

Add an Insta360 Go3S camera adapter to syncfield-python. The adapter triggers recording over BLE (start/stop commands only — camera records autonomously to its own SD card) and retrieves the resulting video files over WiFi (OSC-compatible HTTP on `192.168.42.1`). There is no live preview. Aggregation runs as a background task queue decoupled from the recording session, so users can record continuously while previous episodes download in the background.

## Goals

1. Wireless start/stop of Go3S via BLE from Python, using the reverse-engineered protocol already proven in `opengraph-studio/recorder`.
2. Seamless retrieval of recorded files over WiFi into the session's episode directory.
3. **Atomic per-episode aggregation**: all Go3S files for a given episode land on disk, or the episode is marked failed with the originals preserved on the camera SD card for retry.
4. Support multiple Go3S cameras per session.
5. Viewer integration: dedicated "standalone recorder" stream panel that clearly communicates that live preview is unavailable and shows recording / aggregation status.
6. Background aggregation does not block the recording lifecycle or interfere with subsequent recordings.
7. Minimal, professional UX consistent with the rest of OpenGraph.

## Non-goals

- Live preview / RTMP streaming from the camera.
- Camera firmware updates, or configuration of per-capture settings (resolution/fps). The camera uses its last-set options from its own UI.
- Windows WiFi switching (stub only — raises `NotImplementedError`). Windows BLE control still works.
- Official Insta360 SDK integration. This is a pure reverse-engineered BLE + OSC path.
- Real-time sample emission (no `on_sample` callbacks during recording — Go3S is file-only).

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│ SessionOrchestrator                                             │
│                                                                 │
│  add(Go3SStream("overhead", ble_address=...))                   │
│                                                                 │
│  prepare → connect → start_recording → stop_recording → disc.   │
│                       │                 │                        │
│                       BLE start         BLE stop                 │
│                                         │                        │
│                                         └─► enqueue(episode_id) ─┼──►  AggregationQueue
│                                                                  │          (singleton)
└─────────────────────────────────────────────────────────────────┘
                                                                              │
                                                           ┌──────────────────┘
                                                           ▼
                                                  ┌─────────────────┐
                                                  │ AggregationJob   │
                                                  │                  │
                                                  │ for each camera: │
                                                  │   switch WiFi    │
                                                  │   download files │
                                                  │   restore WiFi   │
                                                  │                  │
                                                  │ atomic per-cam   │
                                                  │ atomic per-job   │
                                                  └─────────────────┘
```

### New package layout

```
src/syncfield/adapters/insta360_go3s/
├── __init__.py              # re-exports Go3SStream
├── stream.py                # Go3SStream (StreamBase subclass)
├── ble/
│   ├── __init__.py
│   ├── protocol.py          # FFFrame, CRC-16/MODBUS, command codes — ported from recorder
│   └── camera.py            # Go3SBLECamera — connect/send/disconnect async helper
├── wifi/
│   ├── __init__.py
│   ├── osc_client.py        # /osc/info, /osc/commands/execute, file download
│   └── switcher.py          # WifiSwitcher ABC + MacWifiSwitcher, LinuxWifiSwitcher, WindowsWifiSwitcher(stub)
└── aggregation/
    ├── __init__.py
    ├── queue.py             # AggregationQueue (singleton), AggregationJob
    └── types.py             # AggregationStatus, AggregationProgress
```

Rationale: the `insta360_go3s/` subpackage (vs a flat file) keeps the ~1000 LOC of BLE protocol + WiFi + aggregation cleanly bounded. Each submodule has one responsibility and is independently testable.

## Core Components

### `Go3SStream(StreamBase)`

Located at `src/syncfield/adapters/insta360_go3s/stream.py`.

Public constructor:
```python
Go3SStream(
    stream_id: str,
    ble_address: str,                      # BLE MAC or CoreBluetooth UUID
    output_dir: Path,                      # episode dir provided by orchestrator
    aggregation_policy: Literal["eager", "on_demand"] = "eager",
    video_mode: Literal["video"] = "video",
)
```

- `kind = "video"`
- `capabilities.live_preview = False` (new field on `StreamCapabilities`)
- `capabilities.produces_file = True`
- `device_key = ("go3s", ble_address)` — orchestrator dedup
- Lifecycle:
  - `prepare()`: resolve BLE address, verify reachability (quick scan probe), no-op otherwise.
  - `connect()`: establish BLE, run sync + auth handshake, disconnect (Connect-Send-Disconnect).
  - `start_recording(session_clock)`: reconnect BLE, send `CMD_START_CAPTURE`, capture host monotonic ns at ACK, disconnect. Return nothing (samples N/A).
  - `stop_recording()`: reconnect BLE, send `CMD_STOP_CAPTURE`, extract SD filepath from response, disconnect. Enqueue an `AggregationJob` if policy ≠ `on_demand`. Return `FinalizationReport` with `file_path=None` initially and `status="pending_aggregation"`.
  - `disconnect()`: cleanup; does not wait for aggregation.
- Health events emitted at key moments (connect/auth ok, start ACK received, stop ACK received, BLE disconnect).
- `discover()` classmethod implementing the existing BLE discovery protocol — matches advertisement names containing `"go 3"` or `"go3"` (case-insensitive).

New `FinalizationReport.status` value: `"pending_aggregation"` (in addition to existing `"completed" | "partial" | "failed"`). Orchestrator treats this as non-terminal for the Go3S stream; the aggregation queue will later update the episode metadata to `"completed"` or `"failed"` atomically.

### BLE Layer (`ble/`)

Direct port from `opengraph-studio/recorder/src/syncfield_recorder/sensors/insta360_ble/`. No behavioral changes. Constants inline:

- Service `0000be80-0000-1000-8000-00805f9b34fb`
- Write `0000be81-…`, Notify `0000be82-…`
- Commands: `START=0x0004`, `STOP=0x0005`, `SET_OPTIONS=0x0002`, `CHECK_AUTH=0x0027`
- FFFrame with CRC-16/MODBUS (polynomial 0xA001, init 0xFFFF)
- Sequence wraps 1–254; 255 reserved for unsolicited notifications
- Auth: send `CHECK_AUTH` with protobuf `[0x0A, len(addr)] + addr + [0x10, 0x02]` where `addr` is the device address
- No persistent heartbeat (camera records autonomously)

`Go3SBLECamera` exposes async: `connect()`, `start_capture() -> int (host_ns at ACK)`, `stop_capture() -> CaptureResult(file_path: str, duration_hint: float)`, `set_video_mode()`, `disconnect()`.

### WiFi Layer (`wifi/`)

`OscHttpClient` (aiohttp-based):
- `probe(timeout=2s)` → GET `/osc/info`, returns camera model
- `list_files(start_position=0, max=100)` → POST `/osc/commands/execute` `camera.listFiles`
- `download(remote_path, local_path, on_progress)` → HTTP GET to `http://192.168.42.1{remote_path}`, fallback ports `6666`, `8080` if 80 fails
- Uses streaming download to a `.part` file then atomic rename on success
- On error: deletes `.part` file, raises

`WifiSwitcher` ABC with methods `current_ssid()`, `connect(ssid, password)`, `restore(ssid)`.
Platform implementations (`MacWifiSwitcher` uses `networksetup`, `LinuxWifiSwitcher` uses `nmcli`, `WindowsWifiSwitcher` raises `NotImplementedError`).

Factory `wifi_switcher_for_platform()` returns the right impl based on `sys.platform`.

### Aggregation Layer (`aggregation/`)

`AggregationQueue` — process-wide singleton that owns a single worker asyncio task.
- `enqueue(job: AggregationJob) -> AggregationJobHandle`
- `status(job_id) -> AggregationStatus`
- `retry(job_id)` — re-enqueue a failed job
- `cancel(job_id)` — only valid while `PENDING`
- `subscribe(listener)` — push updates to viewer WS

`AggregationJob` per (episode, [cameras]): contains list of `(stream_id, ble_address, wifi_ssid, wifi_password, expected_sd_path)`. Serialized to `episode_dir/.aggregation.json` for crash recovery on next SDK start.

Worker loop: pick next job → for each camera, (1) switch WiFi to camera AP, (2) OSC probe to confirm, (3) download the expected SD file returned by `CMD_STOP_CAPTURE` (v1 downloads exactly that one file per camera per episode — no time-window sibling scan), (4) verify file size matches OSC `listFiles` metadata, (5) restore WiFi. If any step fails: delete partial files for that camera, continue to next camera (each camera is an independent sub-step — but the **episode** is still atomic: the final episode status is `completed` only if all cameras succeed; otherwise `failed` and retry re-runs only the failed ones).

**Atomicity guarantee**: per-episode completion is all-or-nothing at the episode_status level. However, per-camera progress is preserved across retries — a retry does not re-download already-verified files.

Progress reporting: each 64KB downloaded triggers a status update. Viewer sees `(bytes_done / total_bytes, current_camera, current_file)` at ~2 Hz.

## Data Model Changes

### `StreamCapabilities` (src/syncfield/stream.py)
Add:
- `live_preview: bool = True` — when False, viewer routes to `StandaloneRecorderPanel`.
- `produces_file: bool = False` — when True, adapter writes binary file(s) into episode_dir; manifest records them. (Implementation note: if an equivalent field already exists on `StreamCapabilities`, reuse it instead of adding a duplicate.)

### `FinalizationReport` (src/syncfield/types.py)
Extend `status` literal union with `"pending_aggregation"`.

### New types (src/syncfield/adapters/insta360_go3s/aggregation/types.py)
```python
class AggregationState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class AggregationProgress:
    job_id: str
    episode_id: str
    state: AggregationState
    cameras_total: int
    cameras_done: int
    current_stream_id: str | None
    current_bytes: int
    current_total_bytes: int
    error: str | None
```

### Episode metadata
Each episode dir gets `aggregation.json`:
```json
{
  "job_id": "agg_20260414_123456_ab12",
  "state": "completed" | "running" | "pending" | "failed",
  "cameras": [
    {"stream_id": "overhead", "sd_path": "/DCIM/Camera01/VID_...mp4",
     "local_file": "overhead.mp4", "bytes": 2147483648, "done": true}
  ],
  "started_at_ns": 1234,
  "completed_at_ns": 5678,
  "error": null
}
```

### `manifest.json` extension
For each Go3S stream, include `original_filename`, `camera_model`, and `aggregation_job_id`.

## Lifecycle & State Machine

Orchestrator state machine (`src/syncfield/orchestrator.py`) unchanged in structure, but stream's own `FinalizationReport.status = "pending_aggregation"` flows through the session-level `SessionReport` so callers know the session is "recorded but aggregation is ongoing."

Aggregation state is independent of session state:
```
PENDING ──► RUNNING ──► COMPLETED
              │
              └──► FAILED ──► (retry) ──► RUNNING ──► ...
```

`aggregation_policy`:
- `eager` (default single-host): on `stop_recording()`, auto-enqueue. Worker starts immediately.
- `on_demand` (auto-forced in multihost leader/follower): do not enqueue; viewer "Aggregate now" button triggers enqueue.

v1 supports `eager` (default) and `on_demand`. Future work: `between_sessions` policy that defers aggregation until the orchestrator returns to IDLE state — would require orchestrator-level coordination.

**Multihost autodetection**: on `session.add(Go3SStream(...))`, if session's role is `LeaderRole` or `FollowerRole`, orchestrator downgrades `eager` → `on_demand` with a health event explaining why.

## Error Handling & Atomicity

| Failure | Behavior |
|---|---|
| BLE connect timeout during start | `start_recording` returns failure, session aborts recording; no aggregation enqueued |
| BLE connect timeout during stop | Retry 3× at 2s intervals; if still fails, mark stream `status="failed"` but do NOT aggregate (file name unknown). Health event. User must power-cycle camera to recover file from SD. |
| WiFi switcher not connecting to AP | 3 retries at 5s; job `FAILED`; originals remain on SD; retry button available |
| OSC probe fails after connect | Treat as switch failure (same path) |
| Download interrupted mid-stream | Delete `.part`, record partial byte count for diagnostics, mark camera's download failed; continue next camera in same job; episode status determined at job end |
| WiFi restore fails | File marked complete; health event "manual WiFi reconnect needed"; user notified via viewer toast. No data loss. |
| Worker crash / SDK restart | On next start, scan episode dirs for `aggregation.json` with state `pending` or `running`; re-enqueue as `pending`. |
| Two cameras on same AP | Not possible — each camera has its own AP; processed sequentially. |
| User closes viewer mid-aggregation | Worker continues; viewer on reconnect shows current progress. |

**Atomicity definition (explicit)**:
- **Per-camera**: success = file exists locally and size matches the OSC-reported size. Content checksum is not verified because the OSC spec on Go3S does not expose one; size match is the strongest integrity signal available without re-downloading. Failure = partial or missing local file is deleted.
- **Per-episode**: aggregation state transitions to `COMPLETED` only when ALL cameras for that episode succeed. If any fail, episode state is `FAILED` with a per-camera breakdown. Retry re-attempts only failed cameras.

## Viewer UX

### New component: `StandaloneRecorderPanel`
File: `src/syncfield/viewer/frontend/src/components/standalone-recorder-panel.tsx`

Routed when `stream.kind === "video" && stream.capabilities.live_preview === false`. Added to `StreamCard` dispatcher (src/syncfield/viewer/frontend/src/components/stream-card.tsx).

Panel shows:
- Title (stream_id) with colored status dot (idle/recording/aggregating/ready/failed)
- Centered muted camera icon
- Primary text: "Standalone recorder"
- Secondary text: "Live preview unavailable"
- Status row (one of):
  - `● Recording  00:24`
  - `↓ Aggregating  45%  (1.2 GB / 2.7 GB)`
  - `✓ Ready  — 2.7 GB`
  - `⚠ Failed — [Retry]`
- Footer: last episode id, duration, resolution (if known from OSC metadata)

### Global aggregation status bar
Persistent strip at top of viewer (below header, above main content):
- Hidden when no active aggregation
- When active: `Aggregating ep_20260414_… · overhead (2/3)  ━━━━━━━━━━━░░░  45% · 1.2 GB / 2.7 GB · [View Details]`
- On failure: red variant `Aggregation failed — [Retry]`
- On completion: green flash for 3s then fade out

### Episode list badges
In the existing episode list UI, each episode gets a new status badge:
- `Pending`, `Aggregating n%`, `Ready`, `Failed — Retry`

### WebSocket protocol extension
Add to `/ws/control` snapshot:
```json
{
  "aggregation": {
    "active_job": { /* AggregationProgress */ } | null,
    "queue_length": 2,
    "recent_jobs": [ /* last 5 AggregationProgress */ ]
  }
}
```

New commands:
- `aggregate_episode(episode_id)` — enqueue on-demand
- `retry_aggregation(job_id)`
- `cancel_aggregation(job_id)` — only while pending

### Discovery modal extension
`src/syncfield/viewer/frontend/src/components/discovery-modal.tsx` gains a pattern match for `"go 3"`/`"go3"` in the device name. Discovered device shows "Insta360 Go3S" label and "Add as Go3S camera" CTA that calls `session.add(Go3SStream(...))` with a server-generated default `stream_id` (e.g., `go3s_cam_1`).

## Testing Strategy

### Unit tests
- `tests/unit/adapters/insta360_go3s/test_ble_protocol.py` — FFFrame encode/decode, CRC-16/MODBUS vectors, command builders. Fixtures ported from recorder tests if they exist.
- `test_osc_client.py` — mocked aiohttp responses for `/osc/info`, `listFiles`, downloads including partial/truncated streams.
- `test_wifi_switcher.py` — each platform impl unit tested with `subprocess.run` mocked; factory selection test per `sys.platform`.
- `test_aggregation_queue.py` — job lifecycle, retry, crash recovery from `aggregation.json`, listener notifications.
- `test_go3s_stream.py` — lifecycle with fake BLE client, policy resolution (eager / on_demand), multihost auto-downgrade.

### Integration tests
- `tests/integration/test_go3s_session_e2e.py` — full session with mocked BLE + mocked OSC server + temp WiFi switcher; verifies episode dir contents, manifest entries, aggregation.json, and finalization reports.
- `tests/integration/test_go3s_aggregation_during_recording.py` — start aggregation of episode N-1 then fire start/stop for episode N; verify no BLE/WiFi interference, both episodes land successfully.
- `tests/integration/test_go3s_atomic_failure.py` — inject WiFi restore failure mid-job; verify per-camera atomicity and episode rollback.

### Hardware-in-the-loop (optional, gated by `SYNCFIELD_GO3S_HARDWARE_TEST=1`)
- `tests/hardware/test_go3s_real.py` — runs against a real Go3S; skipped in CI.

## Platform Matrix

| Platform | BLE | WiFi Switch | Support Level |
|---|---|---|---|
| macOS 13+ | ✅ (bleak/CoreBluetooth) | ✅ (networksetup) | First-class |
| Linux (NetworkManager) | ✅ (bleak/BlueZ) | ✅ (nmcli) | First-class |
| Linux (wpa_supplicant only) | ✅ | ⚠️ `wpa_cli` fallback (best effort) | Best-effort |
| Windows 10+ | ✅ | ❌ `NotImplementedError` | BLE-only |

## Dependencies

Add to `pyproject.toml` optional-dependencies `camera` extra:
- `aiohttp>=3.9` (if not already in deps)
- Existing: `bleak>=0.21`

No new required deps.

## Configuration Surface

### Programmatic (primary)
```python
import syncfield as sf
from syncfield.adapters.insta360_go3s import Go3SStream

session = sf.SessionOrchestrator(host_id="mac_studio", output_dir=Path("./output"))
session.add(Go3SStream(
    stream_id="overhead",
    ble_address="CA:FE:BA:BE:00:01",
))
session.add(Go3SStream(
    stream_id="side",
    ble_address="CA:FE:BA:BE:00:02",
))
```

### Environment variables
- `SYNCFIELD_GO3S_WIFI_TIMEOUT_SEC` (default 30)
- `SYNCFIELD_GO3S_DOWNLOAD_TIMEOUT_SEC` (default 600)
- `SYNCFIELD_GO3S_AGGREGATION_POLICY_DEFAULT` (default "eager")

### Viewer discovery
BLE discovery modal auto-detects Go3S devices by advertisement name prefix.

## Open Questions (resolved during brainstorming)

- ✅ Atomicity: two-phase (recording atomic; aggregation atomic per-episode).
- ✅ Trigger: auto-by-default + per-session override + multihost auto-downgrade to on-demand.
- ✅ WiFi switching: primary-adapter shared (no secondary-dongle support in v1).
- ✅ OS scope: macOS + Linux first-class, Windows stub.
- ✅ Multi-camera: N cameras per session, sequential aggregation per-episode.
- ✅ Declaration: programmatic + `discover()` + viewer modal integration.
- ✅ File naming: `{stream_id}.{mp4|insv}` with originals recorded in manifest.
- ✅ Video mode: fixed "video" submode; per-capture settings use camera's own UI.

## Out of Scope (deferred)

- Live preview / RTMP.
- Dedicated secondary WiFi adapter support.
- Windows WiFi switching.
- Automatic stitching / reframing of `.insv` files (requires Insta360 Studio).
- Battery / storage status polling from camera.
- Multi-camera parallel aggregation (would require multiple WiFi adapters).
- Per-capture resolution/fps control via BLE.

## Success Criteria

1. Single-host user can `session.add(Go3SStream(...))` and record a session; episode dir ends up with `{stream_id}.mp4` + manifest entry; this happens with no viewer intervention within ~60s of stop for a 10s recording.
2. Multiple Go3S cameras in a single session all appear in the episode dir after aggregation.
3. Aggregation of episode N-1 running in background does not interfere with start/stop of episode N.
4. When WiFi download fails, episode is marked `FAILED`, originals are on SD, viewer shows `Retry` button, retry succeeds if camera reachable.
5. Viewer shows clear "Live preview unavailable" message in the stream panel with no ambiguity.
6. Multihost leader session with Go3S does not break mDNS (policy auto-downgrades to `on_demand`).
7. SDK restart during aggregation resumes the pending job on next run.

## Risks

| Risk | Mitigation |
|---|---|
| Go3S firmware change breaks BLE protocol | Pin to tested firmware version in docs; protocol constants isolated in one module for quick patching |
| WiFi switch permission prompts on macOS (Location) | One-time prompt documented in README; graceful error if denied |
| Insta360 legal action on reverse-engineered protocol | Low risk (BLE protocol is well-documented publicly); document that this is for research use |
| User's lab WiFi drops during aggregation on multihost leader | Policy auto-downgrade to on_demand prevents this by default |
| Concurrent aggregation jobs thrashing WiFi | Singleton queue with serial worker — only one active job at a time |
