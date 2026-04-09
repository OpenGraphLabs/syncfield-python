# Replay Viewer — Design Spec

**Date:** 2026-04-09
**Status:** Approved for implementation
**Owner:** styu12

## Problem

SyncField SDK records multi-modal sessions to a local folder, then the
synced result (per-stream offsets, quality report) comes back from
`sync.opengraphlabs.com`. Right now there is no way for an external
customer to **verify that the sync worked** without standing up the
internal egonaut/web dashboard, which depends on Supabase, auth, and
hosted infrastructure they don't have.

We need a **local, zero-infra** way to open a saved session and visually
confirm sync quality — primarily by playing the streams back together
and comparing the **before** (raw recording) and **after** (synced)
states side by side.

## Scope

**In v1 (scope A):**
- Open a local session folder in a browser-based replay viewer
- Show synced multi-stream video playback with a master scrubber
- Toggle between **Before** (raw) and **After** (synced) at any time
- Show a sync report panel: per-stream offset, confidence, quality badge
- Show generic sensor streams as minimal, clean line/area charts
- Read sync result from `synced/sync_report.json` if present

**Out of v1 (deferred):**
- 3D viewers (egomotion, body pose) and `@react-three/*` deps
- Action segments / subtitles
- Hand pose / contact overlays beyond what tactile streams already produce
- i18n (English-only, hardcoded strings)
- Pipeline result visualization
- `launch_passive()` variant
- Multi-session browser ("pick a folder" UI)
- LocalStorage persistence of UI state
- Browser tab close → auto-shutdown detection
- E2E browser tests

## Architecture

```
sf.replay.launch(session_dir)
        │
        ▼
┌─────────────────────────────────────────────────┐
│  syncfield.replay  (Python, [replay] extra)     │
│                                                 │
│  loader.py   → ReplayManifest (manifest +       │
│                  sync_point + sync_report)      │
│  server.py   → Starlette + uvicorn, 127.0.0.1   │
│  static/     → built React SPA (committed)      │
└────────────────────┬────────────────────────────┘
                     │  HTTP
                     ▼
┌─────────────────────────────────────────────────┐
│  Browser SPA  (React + Vite + Tailwind 4)       │
│                                                 │
│  - DataReviewPage shell (ported from egonaut)   │
│  - VideoArea + per-stream <video>               │
│  - SyncReportPanel                              │
│  - BeforeAfterToggle                            │
│  - SensorChartPanel (multi-stream, minimal)     │
│  - ContactTimeline / TactilePanel (conditional) │
└─────────────────────────────────────────────────┘
```

**Design principles:**
- Python serves files + small JSON manifest. No transcoding.
- React SPA is **a port of egonaut/web's DataReviewPage** with the data
  source swapped from Supabase to local HTTP. Visual design is 1:1.
- The two layers' only contract is the JSON shape returned by
  `/api/session` + `/api/sync-report` and the existence of
  Range-supporting media URLs.
- Pre-built `static/` is **committed to the repo** so end users install
  via `pip install 'syncfield[replay]'` and never need Node.js or yarn.

## Python side

### Public API

```python
# src/syncfield/replay/__init__.py
def launch(
    session_dir: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,           # 0 = ephemeral
    open_browser: bool = True,
) -> None:
    """Open a synced-session replay viewer in the default browser.

    Blocks the calling thread. Ctrl+C to stop the server.
    """
```

Mirrors `syncfield.viewer.launch()` shape on purpose. Future
`launch_passive()` is a v2 concern.

### Module layout

```
src/syncfield/replay/
├── __init__.py          # public launch() + import-time dep check
├── loader.py            # session folder → ReplayManifest
├── server.py            # ReplayServer wrapping uvicorn + Starlette
├── _handler.py          # Range-aware media route + path-traversal guard
└── static/              # built frontend (committed, shipped)
    ├── index.html
    └── assets/...
```

### `loader.py`

```python
@dataclass(frozen=True)
class ReplayStream:
    id: str
    kind: Literal["video", "sensor", "custom"]
    media_url: str | None        # /media/<id> for video, else None
    media_path: Path | None      # absolute disk path for video file
    data_url: str | None         # /data/<file> for sensor jsonl, else None
    frame_count: int

@dataclass(frozen=True)
class ReplayManifest:
    session_dir: Path
    host_id: str
    sync_point: dict             # raw sync_point.json
    streams: list[ReplayStream]
    sync_report: dict | None     # raw synced/sync_report.json or None
    has_frame_map: bool          # synced/frame_map.jsonl present?

def load_session(session_dir: Path) -> ReplayManifest:
    """Read manifest.json + sync_point.json + synced/* and normalize.

    Raises FileNotFoundError if manifest.json is missing.
    """
```

**Session folder contract** (consumed; produced by `syncfield.writer`):
- `manifest.json` — required
- `sync_point.json` — optional, warning if missing
- `session_log.jsonl` — optional
- `<stream_id>.mp4` — per video stream
- `<stream_id>.jsonl` — per sensor/custom stream
- `synced/sync_report.json` — optional, written by post-sync workflow
- `synced/frame_map.jsonl` — optional, mapping output

The exact stream-id-to-filename rule must be cross-checked against the
real `syncfield.writer` during implementation step 1; this spec assumes
direct `<id>.<ext>` matching.

### `server.py` — Starlette + uvicorn

**Routes:**

| Method | Path                  | Returns                                       |
| ------ | --------------------- | --------------------------------------------- |
| GET    | `/`                   | `static/index.html`                           |
| GET    | `/assets/*`           | bundled static assets                         |
| GET    | `/api/session`        | `ReplayManifest` JSON (no `media_path`)       |
| GET    | `/api/sync-report`    | `sync_report` JSON or 404                     |
| GET    | `/media/{stream_id}`  | MP4 file with HTTP Range support (FileResponse) |
| GET    | `/data/{filename}`    | Raw file under session_dir, Range optional   |

Starlette's `FileResponse` natively handles Range requests, which is the
whole reason we picked it over stdlib `http.server`.

**Security:**
- Default `host="127.0.0.1"` — never bind external interfaces by default
- Path resolution for `/media/` and `/data/`: `Path(...).resolve()` then
  `is_relative_to(session_dir.resolve())` check; reject anything outside
  with 404. Symlink escape is the main attack to defend against.
- No CORS, no auth (single-origin localhost)

### Lifecycle

```python
def launch(session_dir, *, host, port, open_browser):
    manifest = load_session(Path(session_dir))
    server = ReplayServer(manifest, host=host, port=port)

    if open_browser:
        threading.Thread(
            target=lambda: (time.sleep(0.2), webbrowser.open(server.url)),
            daemon=True,
        ).start()

    try:
        server.serve()       # blocks
    except KeyboardInterrupt:
        pass
```

## Web SPA

### Project layout

```
src/syncfield/replay/_web/        # source, committed, NOT shipped
├── package.json                  # yarn
├── vite.config.ts                # build.outDir = "../static"
├── tsconfig.json
├── tailwind.config.ts            # ported from egonaut/web
├── index.html
└── src/
    ├── main.tsx
    ├── App.tsx                   # DataReviewPage skeleton
    ├── components/
    ├── hooks/
    ├── lib/
    └── types.ts
```

The leading underscore on `_web` keeps the directory out of Python's
package import surface.

### Ported from egonaut/web (keep, modify as noted)

| File                                   | Modification                   |
| -------------------------------------- | ------------------------------ |
| `pages/DataReviewPage.tsx`             | Heavy — drop `useRecordingContext`, swap data hooks |
| `components/review/VideoArea.tsx`      | Light — signed URL → `/media/<id>`, add per-stream offset |
| `components/review/HeroVideo.tsx`      | As-is                          |
| `components/review/SecondaryVideo.tsx` | As-is                          |
| `components/review/ContactTimeline.tsx`| Conditional render             |
| `components/review/TactilePanel.tsx`   | Conditional render             |
| `components/review/SensorWaveform.tsx` | **Generalized** — see below    |
| `components/review/HandOverlay.tsx`    | Conditional render             |
| `hooks/useTactileContact.ts`           | As-is                          |
| `lib/dataParser.ts`, `sensorParser.ts` | As-is                          |
| `index.css`, tailwind tokens           | As-is (1:1 visual fidelity)    |

### Stripped from egonaut/web

- Supabase client (`lib/supabase.ts`), `useAuth`, `useSignedUrl`
- React Router (single screen, no routing)
- `RecordingLayout`, `RecordingCard`, every other page
- `react-intl` (English hardcoded)
- `EgomotionViewer` + `@react-three/*` + `three`
- `BodyOverlay`
- `ActionSubtitle` + action segments

### Data hook replacement

```ts
// _web/src/hooks/useReplaySession.ts
export function useReplaySession() {
  const [session, setSession] = useState<SessionManifest | null>(null);
  const [syncReport, setSyncReport] = useState<SyncReport | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetch("/api/session").then((r) => r.json()),
      fetch("/api/sync-report").then((r) => (r.ok ? r.json() : null)),
    ]).then(([s, r]) => {
      setSession(s);
      setSyncReport(r);
      setLoading(false);
    });
  }, []);

  return { session, syncReport, loading };
}
```

`useRecordingData`'s "fetch jsonl, parse to typed array" logic still
applies — point it at `/data/<file>` and reuse the existing parsers.

### New components

**`SyncReportPanel`** — header right side. Per-stream card:
- Stream id
- Offset, signed (`+0.312s` / `-0.045s`)
- Confidence bar (0..1 → 0..100%)
- Quality badge (excellent/good/fair/poor) using egonaut's `qualityColor()`
- Chirp detected? (icon + ms tooltip)

**`BeforeAfterToggle`** — header center, segmented control
`[ Before | After ]`. Default After. Disabled when no `sync_report`.
Keyboard `B` toggles. Brief visual flash on toggle.

**`SensorChartPanel`** — generalized chart container, replaces what was
tactile-specific in egonaut. Renders one minimal line/area chart per
sensor channel. Shared time axis aligned with the master scrubber.
Vertical playhead line. Style: thin strokes, muted colors, generous
whitespace, no grid lines, axis labels only at extremes — same
aesthetic as egonaut's `SensorWaveform` but as a multi-stream panel.

## Before / After playback math

`sync_report` provides per-stream offsets:

```ts
type SyncStreamResult = {
  stream_id: string;
  offset_seconds: number;     // sign convention TBD vs real API
  confidence: number;         // 0..1
  quality: "excellent" | "good" | "fair" | "poor";
};
```

For master playhead `t_master` (the scrubber value):

- **After** (default, synced): each `<video>.currentTime = t_master - offset[stream]`
- **Before** (raw): each `<video>.currentTime = t_master`, offsets ignored

Result: in After, the chirp peak appears at the same `t_master` across
all streams; in Before, it appears at each stream's native time. The
toggle flip is the verification.

VideoArea's per-video seek effect:

```ts
useEffect(() => {
  const offset = mode === "after" ? (offsets[streamId] ?? 0) : 0;
  video.currentTime = Math.max(0, seekTargetTime - offset);
}, [seekVersion, mode, offsets, streamId]);
```

Drift correction during playback: `requestAnimationFrame` checks each
video's `currentTime` against expected, re-seeks silently if drift
exceeds 50 ms.

## Packaging

### `pyproject.toml`

```toml
[project.optional-dependencies]
viewer = ["dearpygui>=1.10", "numpy>=1.24"]
replay = ["starlette>=0.36", "uvicorn>=0.27"]

[tool.hatch.build.targets.wheel]
packages = ["src/syncfield"]

[tool.hatch.build.targets.wheel.force-include]
"src/syncfield/replay/static" = "syncfield/replay/static"

[tool.hatch.build.targets.sdist]
exclude = [
  "src/syncfield/replay/_web/node_modules",
  "src/syncfield/replay/_web/dist",
]
```

Final exclude rules pinned during implementation against actual hatch
behavior; the leading-underscore convention keeps `_web/` out of the
Python import path regardless.

### Dev workflow

Two terminals while iterating on the frontend:

```bash
# Terminal 1 — Python API server only
python -m syncfield.replay.server --dev ./data/session_... --port 8765

# Terminal 2 — Vite dev server with HMR
cd src/syncfield/replay/_web
yarn dev
```

`vite.config.ts` proxies `/api`, `/media`, `/data` to `localhost:8765`.

### Build & ship

```bash
cd src/syncfield/replay/_web
yarn install
yarn build                  # → ../static/index.html + assets
git add src/syncfield/replay/static
git commit
```

CI gate: when `_web/` changes, run `yarn build` and verify `static/`
diff is empty (i.e. the committed dist matches the source).

### Makefile targets

```make
replay-web-install:
	cd src/syncfield/replay/_web && yarn install

replay-web-build:
	cd src/syncfield/replay/_web && yarn build

replay-web-dev:
	cd src/syncfield/replay/_web && yarn dev
```

## Testing

### Python

- `tests/replay/test_loader.py` — synthesized session folder fixture,
  manifest parsing, with/without `synced/`, missing files
- `tests/replay/test_server.py` — Starlette `TestClient` against every
  route. Range header → 206 partial response verification.
- `tests/replay/test_security.py` — `/media/../etc/passwd` and symlink
  escape attempts must return 404
- `tests/replay/test_launch_smoke.py` — boot `launch()` on a background
  thread with `open_browser=False`, hit `/api/session` via `requests`,
  shut down cleanly

### Web

- `_web/src/hooks/__tests__/useBeforeAfter.test.ts` — pure-function offset math
- `_web/src/components/__tests__/SyncReportPanel.test.tsx` — render,
  no-report state, quality badge mapping

E2E browser tests are out of v1 scope.

## Documentation

- `README.md` — 10-line "Replay a synced session" section
- `docs/replay.md` — session folder contract, supported stream kinds,
  before/after semantics, troubleshooting
