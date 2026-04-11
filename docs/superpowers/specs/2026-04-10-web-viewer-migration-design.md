# Web Viewer Migration Design Spec

**Date:** 2026-04-10
**Status:** Approved
**Scope:** Replace DearPyGui desktop viewer with browser-based web viewer

## Summary

Replace the current DearPyGui desktop GUI viewer with a web browser-based viewer. The Python SDK starts a FastAPI server and opens a browser tab. The web frontend is built with Vite + React + shadcn/ui + Tailwind, porting the design system from the opengraph-studio/recorder project. The public API (`viewer.launch(session)`) remains unchanged.

## Decisions

| Item | Decision |
|------|----------|
| Feature scope | Recorder design system + SyncField SDK features only (no config/task/calibration) |
| Styling | shadcn/ui + Tailwind CSS |
| Code location | Monorepo — `src/syncfield/viewer/frontend/` inside syncfield-python |
| Camera streaming | MJPEG stream |
| Sensor streaming | SSE (Server-Sent Events) |
| Package manager | yarn |
| API design | Hybrid — REST + WebSocket + MJPEG + SSE |
| Dev/deploy split | Vite dev server (dev) → static assets in FastAPI (prod) |

## Architecture

### Data Flow

```
SessionOrchestrator
       │
SessionPoller (10Hz, unchanged)
       │
SessionSnapshot (frozen dataclass, unchanged)
       │
FastAPI Server (server.py)
  ├── WebSocket /ws/control ──→ JSON snapshot broadcast (10Hz)
  │                           ←─ control commands
  ├── MJPEG /stream/video/{id} ──→ latest_frame JPEG continuous stream
  ├── SSE /stream/sensor/{id}  ──→ plot_points data push
  └── REST /api/*              ──→ status, discover, stream management
       │
React App (browser)
  ├── useSession() hook ──→ WebSocket state + commands
  ├── <img src="/stream/video/{id}"> ──→ native MJPEG rendering
  ├── useSensorStream() hook ──→ SSE → chart rendering
  └── useDiscovery() hook ──→ REST scan trigger
```

### Directory Structure

```
src/syncfield/viewer/
├── __init__.py              # launch(), launch_passive() public API (signature unchanged)
├── app.py                   # ViewerApp: FastAPI + uvicorn + webbrowser.open()
├── server.py                # FastAPI app, routing, WebSocket/MJPEG/SSE handlers
├── poller.py                # SessionPoller (unchanged)
├── state.py                 # SessionSnapshot, StreamSnapshot (unchanged)
├── frontend/                # Vite + React project
│   ├── package.json
│   ├── yarn.lock
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── tailwind.config.ts
│   ├── components.json      # shadcn/ui config
│   ├── index.html
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── hooks/
│   │   │   ├── use-session.ts
│   │   │   ├── use-sensor-stream.ts
│   │   │   └── use-discovery.ts
│   │   ├── components/
│   │   │   ├── ui/               # shadcn/ui components (Button, Dialog, Toast, Table)
│   │   │   ├── header.tsx
│   │   │   ├── control-panel.tsx
│   │   │   ├── stream-card.tsx
│   │   │   ├── video-preview.tsx
│   │   │   ├── sensor-chart.tsx
│   │   │   ├── health-table.tsx
│   │   │   ├── session-clock.tsx
│   │   │   ├── discovery-modal.tsx
│   │   │   └── countdown-overlay.tsx
│   │   ├── lib/
│   │   │   ├── types.ts
│   │   │   ├── format.ts
│   │   │   └── utils.ts          # shadcn cn() utility
│   │   └── styles/
│   │       └── globals.css
│   └── __tests__/
│       ├── format.test.ts
│       ├── use-session.test.ts
│       └── use-sensor-stream.test.ts
├── static/                  # vite build output (.gitignore)
│   ├── index.html
│   └── assets/
```

## Python Backend (server.py)

### Public API

```python
def launch(session, *, host="127.0.0.1", port=8420, title="SyncField"):
    """Start web server + open browser, blocking. Ctrl+C to exit."""

@contextmanager
def launch_passive(session, *, host="127.0.0.1", port=8420, title="SyncField"):
    """Background web server. Caller controls session lifecycle."""
```

Signature matches existing DearPyGui API. `host` and `port` are new optional parameters (non-breaking).

### Endpoints

```
WebSocket:
  /ws/control
    server→client: SessionSnapshot JSON (10Hz broadcast)
    client→server: {"action": "connect"|"disconnect"|"record"|"stop"|"cancel"}

MJPEG:
  /stream/video/{stream_id}
    StreamingResponse(multipart/x-mixed-replace)
    latest_frame → cv2.imencode(".jpg") → yield

SSE:
  /stream/sensor/{stream_id}
    EventSource, text/event-stream
    channel values push (~10Hz)

REST:
  GET  /api/status           # current SessionSnapshot (one-shot)
  POST /api/discover         # trigger device scan (async, result via WebSocket)
  POST /api/streams/{id}     # add discovered device to session
  DELETE /api/streams/{id}   # remove stream

Static:
  /*                         # built React app (SPA fallback → index.html)
```

### WebSocket Protocol

**Server → Client (10Hz):**

```json
{
  "type": "snapshot",
  "state": "recording",
  "host_id": "mac_studio",
  "elapsed_s": 12.345,
  "chirp": {"enabled": true, "start_ns": 123456, "stop_ns": null},
  "streams": {
    "mac_webcam": {
      "id": "mac_webcam",
      "kind": "video",
      "frame_count": 370,
      "effective_hz": 29.8,
      "last_sample_ms_ago": 33,
      "provides_audio_track": false,
      "produces_file": true,
      "health_count": 0
    }
  },
  "health_log": [
    {"stream_id": "iphone", "kind": "drop", "at_s": 5.2, "detail": "frame skip"}
  ],
  "output_dir": "...ep_20260410_143022_a1b2c3"
}
```

`latest_frame` and `plot_points` are excluded from WebSocket — they use dedicated MJPEG/SSE channels.

**Server → Client (countdown events):**

```json
{"type": "countdown", "count": 3}
{"type": "countdown", "count": 2}
{"type": "countdown", "count": 1}
```

**Client → Server:**

```json
{"action": "connect"}
{"action": "disconnect"}
{"action": "record", "countdown_s": 3}
{"action": "stop"}
{"action": "cancel"}
```

5 actions only, 1:1 mapping to current DearPyGui viewer buttons.

### snapshot_to_dict()

Converts frozen dataclass to JSON-serializable dict. Excludes `latest_frame` (numpy array) and `plot_points` (deque) to keep WebSocket payload lightweight.

## React Frontend

### Component Tree

```
App
├── Header
│   ├── Logo "SyncField"
│   ├── Host ID
│   ├── State dot + label (● RECORDING)
│   ├── Elapsed timer (MM:SS.mmm)
│   └── "Discover Devices" button → DiscoveryModal
├── ControlPanel
│   ├── Connect / Disconnect buttons
│   ├── Record / Stop buttons
│   └── Cancel button
├── SessionClock
│   ├── sync_point display
│   ├── chirp status
│   └── tone config
├── StreamsSection (horizontal scroll)
│   └── StreamCard (per stream)
│       ├── Header: stream ID + status dot + remove button
│       ├── Tags: kind · audio · file
│       ├── Body (by kind):
│       │   ├── VideoPreview — <img src="/stream/video/{id}">
│       │   ├── SensorChart — SSE + realtime SVG line chart
│       │   └── "no preview" placeholder
│       └── Footer: frame count · Hz · last sample ago
├── HealthTable (shadcn Table)
│   └── Columns: Time | Stream | Kind | Detail
├── Footer
│   ├── output path (right 60 chars, truncated)
│   └── wall clock (ISO datetime)
├── CountdownOverlay (conditional)
│   └── 3 → 2 → 1 large number + animation
└── DiscoveryModal (shadcn Dialog)
    ├── Scan status indicator
    ├── Device list (checkbox + name + adapter info)
    └── Rescan / Close / Add buttons
```

### State Management

```typescript
// hooks/use-session.ts
function useSession(): {
  snapshot: SessionSnapshot | null
  sendCommand: (action: string, data?: object) => void
  connectionStatus: "connecting" | "connected" | "disconnected"
}
```

Single WebSocket connection. Reconnect on disconnect. All control via `sendCommand()`.

### Sensor Streaming

```typescript
// hooks/use-sensor-stream.ts
function useSensorStream(streamId: string): {
  channels: Record<string, number[]>
  labels: number[]
  isConnected: boolean
}
```

EventSource (SSE). Per-channel rolling buffer, max 300 points. SensorChart renders custom SVG directly — no chart library dependency.

### Audio Feedback

Audio playback architecture:

- **Recording PC (Python/sounddevice):** Plays countdown ticks (C6, 1047Hz, 100ms) and chirps (400-2500Hz FM sweep, 500ms) via PortAudio. Captured by microphones for cross-correlation sync. Completely unchanged by this migration.
- **Browser (Web Audio API):** Plays countdown tick sounds (C6, 1047Hz, 100ms) for user feedback only. Does NOT play chirps — chirps serve physical capture purposes and are only meaningful on the recording PC.

Countdown tick in browser is triggered by WebSocket `{"type": "countdown", "count": N}` messages.

### Design Tokens

Recorder's CSS custom properties mapped to Tailwind config:

```typescript
// tailwind.config.ts
{
  colors: {
    background: "hsl(60 7% 95%)",     // Recorder --bg
    foreground: "hsl(0 0% 13%)",      // Recorder --text
    primary: "hsl(153 35% 38%)",      // Recorder --primary (teal)
    muted: "hsl(0 0% 42%)",           // Recorder --text-secondary
    destructive: "hsl(0 65% 48%)",    // Recorder --status-rec
  },
  fontFamily: {
    sans: ["Inter", "system-ui", "sans-serif"],
  }
}
```

shadcn/ui components (Button, Dialog, Table, Toast) use Recorder's color palette.

### Testing

| Target | Tool | Scope |
|--------|------|-------|
| `format.ts` | Vitest | Pure function unit tests |
| `use-session.ts` | Vitest + mock WebSocket | Connection/reconnect, message parsing, command sending |
| `use-sensor-stream.ts` | Vitest + mock EventSource | SSE connection, rolling buffer, disconnect |
| `types.ts` | Vitest | Type guard/parser functions |

UI component render tests (React Testing Library) excluded from initial scope.

## Build & Deploy

### Development

```
Terminal 1:  cd src/syncfield/viewer/frontend && yarn dev
             → Vite dev server :5173 (HMR)

Terminal 2:  python examples/iphone_mac_webcam/record.py
             → FastAPI :8420
             → Browser opens :5173, API proxied to :8420
```

```typescript
// vite.config.ts
export default defineConfig({
  server: {
    proxy: {
      "/ws": { target: "ws://localhost:8420", ws: true },
      "/api": "http://localhost:8420",
      "/stream": "http://localhost:8420",
    },
  },
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
})
```

### Production (pip install)

```
pip install syncfield[viewer]
→ FastAPI serves viewer/static/ built assets
→ webbrowser.open("http://localhost:8420")
```

### pyproject.toml

```toml
[project.optional-dependencies]
viewer = [
    "fastapi>=0.104.0",
    "uvicorn[standard]>=0.24.0",
    "opencv-python>=4.8.0",
]
# dearpygui removed

[tool.setuptools.package-data]
"syncfield.viewer" = ["static/**/*"]
```

### Build Command

```makefile
build-viewer:
	cd src/syncfield/viewer/frontend && yarn install --frozen-lockfile && yarn build
```

`static/` directory is in `.gitignore`. CI runs `yarn build` before Python package build.

## Migration: What Changes

### Removed

| File | Reason |
|------|--------|
| `viewer/theme.py` | Replaced by Tailwind design tokens |
| `viewer/fonts.py` | Browser font loading |
| `viewer/widgets/` (entire directory) | React components |

### Rewritten

| File | Change |
|------|--------|
| `viewer/app.py` | DearPyGui context/render loop → FastAPI + uvicorn + webbrowser.open() |
| `viewer/__init__.py` | Internal implementation swap only, public signature preserved |
| `viewer/demo.py` | Remove DearPyGui dependency, use web viewer |

### Unchanged

| File | Reason |
|------|--------|
| `viewer/poller.py` | Web server consumes SessionPoller identically |
| `viewer/state.py` | SessionSnapshot/StreamSnapshot shared by both backends |

### Added

| File | Purpose |
|------|---------|
| `viewer/server.py` | FastAPI app with all endpoints |
| `viewer/frontend/` | Vite + React project |
| `viewer/static/` | Build output (.gitignore) |

### Dependencies

```
Removed: dearpygui
Added: fastapi>=0.104.0, uvicorn[standard]>=0.24.0
Kept: opencv-python>=4.8.0 (already used by adapters)
```

### Backward Compatibility

- `syncfield.viewer.launch(session)` — same signature, same blocking behavior
- `syncfield.viewer.launch_passive(session)` — same context manager pattern
- `host`, `port` parameters are new optionals (non-breaking)
- All `examples/` user code unchanged
