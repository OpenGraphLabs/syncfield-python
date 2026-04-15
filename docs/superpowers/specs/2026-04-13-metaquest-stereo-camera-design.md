# Meta Quest 3 Stereo Camera Adapter — Design

**Status:** Draft — awaiting user approval
**Date:** 2026-04-13
**Branch:** `feat/metaquest-stereo-camera`
**Builds on:** `feat/metaquest-clock-domain-health-events` (adds `clock_domain`/health events to `MetaQuestHandStream`)

---

## 1. Context & Goal

The existing `MetaQuestHandStream` adapter receives hand-tracking + head-pose JSON over UDP from a Unity app running on a Meta Quest 3 headset. We want to extend the Quest ↔ Macbook pipeline so that the Quest's **two front-facing RGB passthrough cameras** are also captured during a SyncField session, producing egocentric stereo video that is time-aligned with the existing tracking streams.

### Requirements

| # | Requirement |
|---|---|
| R1 | Capture both left and right Quest 3 passthrough RGB cameras at **720p × 30 fps** each |
| R2 | **Hybrid recording mode**: low-resolution preview streamed live + high-resolution video recorded on-device and transferred after `stop_recording()` |
| R3 | High-resolution stereo video must arrive on the host **without frame loss** (hybrid mode eliminates UDP packet-loss risk) |
| R4 | Per-frame timestamps must be aligned with other session streams under `clock_domain="remote_quest3"`, `uncertainty_ns=10_000_000` (10 ms WiFi jitter) |
| R5 | One `MetaQuestCameraStream` adapter exposes **both** cameras as one logical device (matches `OakCameraStream`'s multi-output pattern) |
| R6 | Implementation must be **readable and extensible** — single-responsibility Unity components, tested Python units |
| R7 | Must **not regress** the existing `UDPTrackingSender.cs` tracking flow |

### Non-Goals (out of scope for this spec)

- Depth / SLAM fish-eye cameras (not accessible via PCA)
- On-device ML inference or stereo rectification
- Dynamic bitrate / adaptive quality control
- Audio capture from Quest microphone
- Recovery from mid-session Quest app crash (session is aborted; user re-records)

---

## 2. Architecture Overview

```
┌──────────────────────────────────────┐              ┌───────────────────────────────────┐
│  Macbook (syncfield-python SDK)      │              │  Meta Quest 3 (Unity app)         │
│                                      │              │                                   │
│  ┌──────────────────────────────┐    │  UDP 14043   │  ┌──────────────────────────┐     │
│  │ MetaQuestHandStream          │ ◀──┼──────────────┼─ │ UDPTrackingSender.cs     │     │
│  │   (existing)                 │    │              │  │   (existing, unchanged)  │     │
│  └──────────────────────────────┘    │              │  └──────────────────────────┘     │
│                                      │  UDP 14044   │  ┌──────────────────────────┐     │
│  ┌──────────────────────────────┐    │ (discovery)  │  │ (existing discovery      │     │
│  │ Discovery / mDNS             │ ◀──┼──────────────┼─ │  response path)          │     │
│  └──────────────────────────────┘    │              │  └──────────────────────────┘     │
│                                      │              │                                   │
│  ┌──────────────────────────────┐    │  HTTP 14045  │  ┌──────────────────────────┐     │
│  │ MetaQuestCameraStream  (NEW) │ ◀──┼──────────────┼─▶│ CameraHttpServer.cs (NEW)│     │
│  │   - control client           │    │              │  │   - control endpoints    │     │
│  │   - MJPEG client (left)      │    │              │  │   - MJPEG endpoints      │     │
│  │   - MJPEG client (right)     │    │              │  │   - file download        │     │
│  │   - MP4 puller               │    │              │  └──────────┬───────────────┘     │
│  └──────────────────────────────┘    │              │             │                     │
│                                      │              │  ┌──────────▼───────────────┐     │
│                                      │              │  │ PassthroughCameraRecorder│     │
│                                      │              │  │   (NEW)                  │     │
│                                      │              │  │   - PCA capture (L + R)  │     │
│                                      │              │  │   - H.264 HW encode →MP4 │     │
│                                      │              │  │   - JPEG preview (L + R) │     │
│                                      │              │  │   - frame ts JSONL       │     │
│                                      │              │  └──────────────────────────┘     │
│                                      │              │  ┌──────────────────────────┐     │
│                                      │              │  │ SessionCoordinator.cs(NEW)│    │
│                                      │              │  │   - routes /start /stop  │     │
│                                      │              │  └──────────────────────────┘     │
└──────────────────────────────────────┘              └───────────────────────────────────┘
```

### Transport channels

| Port | Protocol | Direction | Purpose | Status |
|------|----------|-----------|---------|--------|
| 14043 | UDP | Quest → Mac | Tracking packets (head/hand/controller) | **existing** |
| 14044 | UDP | Mac → Quest (broadcast) + Quest → Mac | Discovery | **existing** |
| 14045 | TCP/HTTP | Mac → Quest + Quest → Mac | Control + MJPEG preview + MP4 file transfer | **NEW** |

### Bandwidth budget

| Stream | Bandwidth |
|---|---|
| Existing tracking | ~2.4 Mbps |
| MJPEG preview (320×240 × 2 eyes @ 15 fps, q50) | ~2 Mbps |
| On-device recording (no network cost during session) | 0 Mbps |
| Post-session file transfer (burst, ~3 min session ≈ 300–500 MB each) | ~100+ Mbps for a few seconds |
| **Steady-state total** | ~4.4 Mbps — comfortable on WiFi 5 |

---

## 3. Unity-Side Design

Unity app code lives in `opengraph-studio/unity/SyncFieldQuest3Sender/Assets/Scripts/`. The new components are **independent MonoBehaviours** — the existing `UDPTrackingSender.cs` stays untouched.

### 3.1 Component Responsibilities

```
Assets/Scripts/
├── UDPTrackingSender.cs              (existing, NO CHANGE)
│
├── PassthroughCameraRecorder.cs      (NEW) — camera capture + encoding
│   ├── Acquires PCA access via Meta XR SDK (both cameras)
│   ├── Runs 2 per-camera capture coroutines @ 30 fps
│   ├── For each frame:
│   │   ├── Hardware H.264 encode → append to local MP4 (MediaCodec)
│   │   ├── JPEG encode preview variant (downsampled) → enqueue for HTTP
│   │   └── Append (frame_number, capture_ns) to timestamps JSONL
│   └── Exposes start(session_id) / stop() → returns file paths
│
├── CameraHttpServer.cs               (NEW) — HTTP surface on port 14045
│   ├── Uses System.Net.HttpListener (built into Mono/.NET)
│   ├── Routes:
│   │   POST  /recording/start        → SessionCoordinator.StartRecording()
│   │   POST  /recording/stop         → SessionCoordinator.StopRecording()
│   │   GET   /recording/files/{side} → stream MP4 (supports Range headers)
│   │   GET   /recording/timestamps/{side} → stream JSONL
│   │   GET   /preview/{side}         → MJPEG multipart stream (long-lived)
│   │   GET   /status                 → JSON snapshot
│   │   DELETE /recording/files       → cleanup after Python confirms
│   └── Concurrent request handling via HttpListener's built-in async
│
└── SessionCoordinator.cs             (NEW) — state machine tying recorder + HTTP
    ├── Holds current session state (Idle / Recording / Uploading)
    ├── Rejects overlapping sessions
    └── Single source of truth for the recorder lifecycle
```

### 3.2 On-device file layout

During a session, Quest writes to its internal app-scoped storage:

```
{Application.persistentDataPath}/syncfield_recordings/{session_id}/
├── left.mp4
├── right.mp4
├── left.timestamps.jsonl       # one line per frame: {"frame_number": N, "capture_ns": ...}
└── right.timestamps.jsonl
```

`session_id` is passed by the Python side in the `/recording/start` request body (so host and device agree on the same ID).

### 3.3 Timestamps

Quest's `Time.realtimeSinceStartupAsDouble` (or `AudioSettings.dspTime`) does not use host monotonic time. To align with `clock_domain="remote_quest3"` in the Python SDK (which already carries a 10 ms uncertainty for WiFi jitter):

1. `POST /recording/start` body includes `host_mono_ns` — Python's `time.monotonic_ns()` at the moment the request is sent.
2. Unity captures `quest_mono_ns_at_start = Now()` immediately on receipt and stores the offset `delta = host_mono_ns - quest_mono_ns_at_start`.
3. Every frame's `capture_ns` is recorded as `quest_mono_ns_now + delta` — i.e. **projected into the host monotonic domain** before being written to the timestamps JSONL.
4. The Python adapter emits `SampleEvent(capture_ns=<from JSONL>, clock_domain="remote_quest3", uncertainty_ns=10_000_000)`.

### 3.4 Hardware encoding

- **Codec**: H.264 Baseline, 3 Mbps/eye target, 30 fps, 720p.
- **Encoder**: Android MediaCodec surface encoder. Unity plugin options evaluated during plan phase:
  - Option 1: write a thin Java/Kotlin Android plugin that exposes `BeginEncode / SubmitFrame / EndEncode`.
  - Option 2: reuse an existing open-source Unity MediaCodec wrapper (e.g. `com.unity.mobile.video-recorder` if suitable).
  - Decision deferred to implementation plan.

### 3.5 Preview encoding

Preview path is CPU-side to keep MediaCodec free for the main recording:

- Downsample 720p → 320×240 (nearest/bilinear).
- Unity's `ImageConversion.EncodeToJPG(..., quality=50)` per frame at 15 fps (every other frame).
- Push JPEG bytes into a size-1 ring buffer per camera. HTTP server consumer reads from the ring buffer; if consumer is slow, new frames overwrite old ones (preview prioritises latest over completeness).

---

## 4. Python SDK Adapter Design

### 4.1 Public API

```python
from syncfield.adapters import MetaQuestCameraStream

session.add(MetaQuestCameraStream(
    id="quest_cam",
    quest_host="192.168.1.42",   # or None → use discovery
    quest_port=14045,
    fps=30,
    resolution=(1280, 720),
    preview_fps=15,
))

# After session:
#   {output_dir}/quest_cam_left.mp4
#   {output_dir}/quest_cam_right.mp4
#   {output_dir}/quest_cam_left.timestamps.jsonl
#   {output_dir}/quest_cam_right.timestamps.jsonl
```

### 4.2 Class structure (internal decomposition)

One public adapter class composed of small single-purpose collaborators. Each collaborator is unit-testable in isolation using `httpx.MockTransport` / fakes; no Unity required.

```
src/syncfield/adapters/meta_quest_camera/
├── __init__.py                 # exports MetaQuestCameraStream
├── stream.py                   # MetaQuestCameraStream(StreamBase)  — orchestrates
├── http_client.py              # QuestHttpClient — thin wrapper over httpx
├── preview.py                  # MjpegPreviewConsumer — background MJPEG reader,
│                               #   exposes latest_frame_left / latest_frame_right
├── file_puller.py              # RecordingFilePuller — streams MP4 + ts JSONL to disk
└── timestamps.py               # per-frame SampleEvent emission from timestamps JSONL
```

All internal pieces live under `meta_quest_camera/` to keep the top-level `adapters/` directory readable and to group related code. Public SDK imports continue to work via `from syncfield.adapters import MetaQuestCameraStream` (re-exported in `adapters/__init__.py`).

### 4.3 Lifecycle — 4-phase integration

| Phase | Adapter action |
|---|---|
| `prepare()` | no-op |
| `connect()` | GET `/status` → verify Quest is reachable. Start `MjpegPreviewConsumer` threads for both cameras (preview flows during live view). |
| `start_recording(clock)` | `POST /recording/start` with `{session_id, host_mono_ns: clock.sync_point.monotonic_ns, resolution, fps}`. Spawn a tail-reader thread that watches Quest's `/recording/timestamps/{side}` as it grows (chunked transfer) and emits one `SampleEvent` per frame. |
| `stop_recording()` | `POST /recording/stop`. Wait for final timestamps line. Pull `left.mp4` + `right.mp4` via HTTP GET to `{output_dir}/quest_cam_{side}.mp4`. Then pull timestamps JSONL to `{output_dir}/quest_cam_{side}.timestamps.jsonl`. Verify file sizes + last line match `/stop` response. `DELETE /recording/files` on success. Return `FinalizationReport` with status `completed` / `partial` / `failed`. |
| `disconnect()` | Stop preview threads. |

### 4.4 `StreamCapabilities`

```python
StreamCapabilities(
    provides_audio_track=False,
    supports_precise_timestamps=True,  # Quest→host clock projection absorbs jitter
    is_removable=True,                 # WiFi device
    produces_file=True,                # emits MP4 files
)
```

### 4.5 Timestamp files and `SampleEvent` emission

Two distinct concerns, kept separate by design:

**(a) Authoritative per-eye frame timestamps — written by the adapter directly.**
The adapter pulls `left.timestamps.jsonl` and `right.timestamps.jsonl` from the Quest during `stop_recording()` and writes them verbatim (after the `delta_ns` projection already applied on Quest side) into the session `output_dir` as `quest_cam_left.timestamps.jsonl` and `quest_cam_right.timestamps.jsonl`. This mirrors how `OakCameraStream` writes the `.depth.bin` side-channel directly — the orchestrator's per-stream sample writer is bypassed for these files because it only supports one file per `stream_id`.

Each line:
```json
{"frame_number": 0, "capture_ns": 123456789012345, "clock_domain": "remote_quest3", "uncertainty_ns": 10000000}
```

**(b) `SampleEvent` stream — for live-view liveness and orchestrator sample counts.**
The adapter registers one `stream_id="quest_cam"` with the orchestrator and emits one `SampleEvent` per left/right **synchronized pair** as frames arrive (tail-read from Quest's chunked `/recording/timestamps/{side}` endpoint during recording). The event carries:
- `frame_number`: monotonic across the session (0, 1, 2, …)
- `capture_ns`: the timestamp of the left frame in the pair (right-eye timestamp differs by < 1 ms in practice; recorded authoritatively in the per-eye JSONL)
- `clock_domain="remote_quest3"`, `uncertainty_ns=10_000_000`

No `channels` field — video streams don't emit sample channels.

This two-track design keeps the adapter compatible with the existing orchestrator contract while still producing the authoritative per-eye artefacts downstream sync tooling expects.

### 4.6 `latest_frame` properties

```python
@property
def latest_frame_left(self) -> np.ndarray | None: ...
@property
def latest_frame_right(self) -> np.ndarray | None: ...
```

Returns the most recent decoded preview JPEG as BGR `np.ndarray`, thread-safe via a lock. Viewer UI can display both side-by-side.

### 4.7 `device_key`

```python
@property
def device_key(self) -> DeviceKey:
    return ("meta_quest_camera", self._quest_host_identity)
```

Where `_quest_host_identity` is the Quest hostname / discovery-provided stable ID — same device can only be registered once per session.

---

## 5. Wire Protocol Specifications

### 5.1 `POST /recording/start`

**Request body:**
```json
{
  "session_id": "ep_20260413_185230_abc123",
  "host_mono_ns": 123456789012345,
  "resolution": {"width": 1280, "height": 720},
  "fps": 30
}
```

**Response 200:**
```json
{
  "session_id": "ep_20260413_185230_abc123",
  "quest_mono_ns_at_start": 98765432100,
  "delta_ns": 24691356912245,
  "started": true
}
```

**Response 409 (already recording):** `{"error": "session_already_active"}`

### 5.2 `POST /recording/stop`

**Request body:** `{}`

**Response 200:**
```json
{
  "session_id": "ep_20260413_185230_abc123",
  "left":  {"frame_count": 5432, "bytes": 201334528, "last_capture_ns": 234567890123},
  "right": {"frame_count": 5432, "bytes": 201455104, "last_capture_ns": 234567890456},
  "duration_s": 181.05
}
```

### 5.3 `GET /recording/files/{left|right}`

Returns MP4 bytes with `Content-Type: video/mp4`, `Content-Length`, supports `Range` for resume.

### 5.4 `GET /recording/timestamps/{left|right}`

Returns JSONL with `Content-Type: application/x-ndjson`. Each line:

```json
{"frame_number": 0, "capture_ns": 123456789012345}
```

During recording this endpoint returns chunked `Transfer-Encoding: chunked` so the Python adapter can tail it for live `SampleEvent` emission (ends when `/stop` completes).

### 5.5 `GET /preview/{left|right}`

Long-lived HTTP response:
```
Content-Type: multipart/x-mixed-replace; boundary=syncfield
```

Each part:
```
--syncfield
Content-Type: image/jpeg
Content-Length: <N>
X-Frame-Capture-Ns: <int>

<JPEG bytes>
```

### 5.6 `GET /status`

```json
{
  "recording": false,
  "session_id": null,
  "last_preview_capture_ns": 123456789012345,
  "left_camera_ready": true,
  "right_camera_ready": true,
  "storage_free_bytes": 4567890123
}
```

### 5.7 `DELETE /recording/files`

Deletes the `{session_id}` directory. Returns 204 on success.

---

## 6. Error Handling

| Failure | Detection | Recovery |
|---|---|---|
| Quest unreachable at `connect()` | HTTP connection error on `/status` | Raise; orchestrator surfaces error to user |
| Preview MJPEG stream drops | `httpx.ReadTimeout` / connection close | Auto-reconnect loop in `MjpegPreviewConsumer`; emit `HealthEvent(DROP)` then `RECONNECT` |
| `/recording/start` returns 409 | HTTP status | Raise — prior session not cleaned up |
| Quest runs out of storage mid-session | Quest responds to next `/status` with `storage_free_bytes < threshold` | Emit `HealthEvent(WARNING)`; do not auto-stop (user decides) |
| File pull fails partway | `httpx` exception during streaming write | Retry with `Range` header 3×; on permanent failure return `FinalizationReport(status="partial", error=...)` |
| Quest clock drift during session | N/A at recording time | Uncertainty budget (10 ms) covers normal WiFi jitter; drift larger than that is a future problem |
| Quest app crashes mid-session | Health watchdog: no preview frame for > 5 s while `recording=true` | Emit `HealthEvent(ERROR)`; `stop_recording()` returns `status="failed"` |

---

## 7. Testing Strategy

### 7.1 Python unit tests (no Unity)

Built on `httpx.MockTransport` or `respx` to fake the Quest HTTP surface:

| Test module | Coverage |
|---|---|
| `tests/unit/adapters/meta_quest_camera/test_http_client.py` | `/start`, `/stop`, `/status` request shaping; JSON marshalling; 409 handling |
| `tests/unit/adapters/meta_quest_camera/test_preview.py` | MJPEG multipart parsing (normal + malformed); `latest_frame_*` thread safety; reconnect logic |
| `tests/unit/adapters/meta_quest_camera/test_file_puller.py` | File streaming to disk; Range-resume on mock 206 responses; byte-count verification |
| `tests/unit/adapters/meta_quest_camera/test_timestamps.py` | JSONL tail parsing; `SampleEvent` emission with `clock_domain="remote_quest3"`, correct `uncertainty_ns` |
| `tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py` | Full adapter 4-phase lifecycle against a fake HTTP server (in-process, no network); verify MP4 + JSONL land in output_dir |

### 7.2 Integration tests (local, no real Quest)

| Test | Approach |
|---|---|
| `tests/integration/adapters/test_meta_quest_camera_e2e.py` | Spin up a small `aiohttp` test server that mimics the Quest's HTTP surface (serves a pre-recorded MP4 + a fake MJPEG stream). Run the adapter end-to-end through `SessionOrchestrator`. Verify output artifacts match expectations. |

### 7.3 Unity-side tests

- **Unit**: `NUnit` tests in Unity Test Runner for the frame-timestamp projection logic (`delta_ns` math) and the HTTP route dispatch.
- **Manual QA**: a checklist that a developer runs on a physical Quest 3 before any release — documented in the feature PR.

### 7.4 Concurrency / feasibility probe

The very first thing to build (before any production code): a Unity-only throwaway scene that exercises `hand tracking + PCA 2-camera capture + H.264 HW encode + HTTP serve` simultaneously for 3 minutes and logs sustained fps per subsystem. If it cannot sustain 30 fps + 72 Hz tracking, we fall back and tune (lower target fps, drop hand-tracking resolution, etc.) before building the full feature.

---

## 8. Implementation Order (high-level; detailed plan separate)

1. **Feasibility probe** (Unity scene measuring sustained fps) — 1 day
2. **Unity: `PassthroughCameraRecorder`** (single camera, MP4 + timestamps JSONL, no HTTP) — 1–2 days
3. **Unity: extend to stereo** (both cameras) — 0.5 day
4. **Unity: `CameraHttpServer`** (control + file endpoints, no preview yet) — 1 day
5. **Python: `QuestHttpClient` + `RecordingFilePuller`** + unit tests — 1 day
6. **Python: `MetaQuestCameraStream` skeleton** with start/stop/pull (no preview) + unit + integration tests — 1.5 days
7. **Unity: MJPEG preview endpoint** + **Python: `MjpegPreviewConsumer`** + tests — 1 day
8. **Viewer integration** (show both previews) — 0.5 day
9. **End-to-end QA on real hardware** + docs — 1 day

Total: ~8–9 engineering days.

---

## 9. Open Questions / Risks

| # | Item | Mitigation |
|---|---|---|
| Q1 | Does Meta PCA support simultaneous access to both cameras while hand tracking is active? Documentation says yes ("simultaneous access to both cameras"); needs empirical verification. | Feasibility probe (step 1) |
| Q2 | Android MediaCodec Unity plugin — build our own vs. reuse? | Evaluate during plan phase; decision recorded in implementation plan |
| Q3 | Quest storage size limits for long sessions — at 6 Mbps (2× 3 Mbps) a 30-min session = ~1.35 GB. Safe within Quest 3's internal storage, but need low-storage warning. | `/status` endpoint already exposes `storage_free_bytes`; UI surfaces warning |
| Q4 | Should the existing `MetaQuestHandStream` and the new `MetaQuestCameraStream` share a single "Quest device" abstraction? | Out of scope for this spec; they remain independent adapters. A future refactor can introduce a `QuestDevice` facade if needed. |
| Q5 | WiFi bandwidth for post-session file transfer on slow networks | User-visible progress bar; transfer can resume with Range headers; acceptable to wait 30s on slow networks |

---

## 10. Approval

- [ ] User approves design
- [ ] Spec self-review complete (placeholders, consistency, scope, ambiguity)
- [ ] User reviews written spec
- [ ] Transition to `writing-plans` skill for detailed implementation plan
