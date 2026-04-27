# syncfield-python

Multi-modal capture orchestration for Physical AI. Drive cameras, IMUs, and custom sensors through one atomic recording lifecycle, get crash-safe per-stream timestamp logs, and produce episode directories the [SyncField sync service](https://opengraphlabs.com) aligns to sub-frame precision.

Docs: **[opengraphlabs.com/docs](https://opengraphlabs.com/docs)**

## Install

```bash
pip install syncfield
```

The default install ships `UVCWebcamStream` and the browser viewer. Optional adapters are opt-in:

| Need | Install |
|---|---|
| USB / Continuity cameras + viewer + audio chirps | `pip install syncfield` |
| BLE IMU sensors | `pip install "syncfield[ble]"` |
| Off-host cameras (Quest, Insta360 Go3S) | `pip install "syncfield[camera]"` |
| OAK-D depth cameras | `pip install "syncfield[oak]"` |
| Multi-host leader/follower over mDNS | `pip install "syncfield[multihost]"` |
| Everything | `pip install "syncfield[all]"` |

Importing an adapter whose extra is missing raises `ImportError` with the install hint.

## Minimal example

```python
from pathlib import Path

import syncfield as sf
import syncfield.viewer
from syncfield.adapters import UVCWebcamStream

session = sf.SessionOrchestrator(
    host_id="mac_studio",
    output_dir=Path(__file__).parent / "output",
)
out = session.output_dir

session.add(UVCWebcamStream("mac_webcam", device_index=0, output_dir=out))
session.add(UVCWebcamStream("iphone",     device_index=1, output_dir=out))

syncfield.viewer.launch(session)
```

```bash
python record.py
```

Per-episode output under `./output/<episode_id>/`:

```
sync_point.json                 # Time anchor + chirp metadata
manifest.json                   # Per-stream metadata
session_log.jsonl               # Crash-safe timeline
mac_webcam.mp4
mac_webcam.timestamps.jsonl
iphone.mp4
iphone.timestamps.jsonl
```

That directory is the sync service's input format. No conversion step.

## Lifecycle

```
add() → connect() → start() → RECORDING → stop() → finalized episode dir
                      |                      |
                      └── start chirp        └── stop chirp
```

Each device is wrapped in a `Stream` adapter with a fixed SPI: `prepare → connect → start_recording → stop_recording → disconnect`. The orchestrator drives all adapters through one atomic state machine. If any stream fails to come up, the rest are rolled back so no partial episode lands on disk. Start and stop chirps become the cross-host alignment anchor in multi-host mode.

Shipped adapters: `UVCWebcamStream`, `BLEImuGenericStream`, `OakCameraStream`, `MetaQuestCameraStream`, `MetaQuestHandStream`, `Go3SStream` (Insta360), `OgloTactileStream`, `HostAudioStream`, `JSONLFileStream`, `PollingSensorStream`, `PushSensorStream`.

## Multi-host

```bash
pip install "syncfield[multihost]"
```

```bash
# Leader
python examples/multihost_lab/leader.py

# Every other host
python examples/multihost_lab/follower.py
```

After the leader's `stop()`, `session.collect_from_followers()` pulls every follower's files into one canonical tree. See [`examples/multihost_lab/`](examples/multihost_lab/).

## Documentation

| Guide | Covers |
|---|---|
| [Quick Start](https://opengraphlabs.com/docs/quickstart) | iPhone+Mac and Quest+IMUs recipes |
| [Python SDK](https://opengraphlabs.com/docs/sdk/python) | `SessionOrchestrator` lifecycle, Stream SPI |
| [Device Adapters](https://opengraphlabs.com/docs/adapters) | Per-adapter constructors and authoring |
| [Web Viewer](https://opengraphlabs.com/docs/sdk/viewer) | Record / Review modes, passive embedding |
| [Multi-Host Sessions](https://opengraphlabs.com/docs/sdk/multi-host) | Leader/follower, mDNS, cross-host alignment |
| [Device Discovery](https://opengraphlabs.com/docs/sdk/discovery) | Auto-enumerate attached hardware |
| [Concepts](https://opengraphlabs.com/docs/concepts) | Pipeline, hosts, streams, acoustic anchor |
| [API Reference](https://opengraphlabs.com/docs/api-reference) | Sync service REST API |

## Output format

### `sync_point.json`

```json
{
  "sdk_version": "0.3.14",
  "monotonic_ns": 1234567890123456789,
  "wall_clock_ns": 1709890101000000000,
  "host_id": "mac_studio",
  "timestamp_ms": 1709890101000,
  "iso_datetime": "2024-03-08T12:00:01.000000"
}
```

Optional fields: `chirp_start_ns` / `chirp_stop_ns` / `chirp_spec` (when a chirp was played), `session_id` / `role` (multi-host).

### `{stream_id}.timestamps.jsonl`

One JSON object per line.

```jsonl
{"frame_number":0,"capture_ns":1234567890123456789,"clock_source":"host_monotonic","clock_domain":"mac_studio"}
{"frame_number":1,"capture_ns":1234567890156789012,"clock_source":"host_monotonic","clock_domain":"mac_studio"}
```

| Field | Type | Meaning |
|-------|------|---------|
| `frame_number` | int | 0-based sequential index |
| `capture_ns` | int | Monotonic ns at data arrival |
| `clock_source` | string | Typically `"host_monotonic"` |
| `clock_domain` | string | Matches `host_id` for host-clocked streams |

`capture_ns` is monotonically non-decreasing within a stream. `clock_domain` is identical across host-clocked streams on the same host. File name must be the literal `{stream_id}.timestamps.jsonl`.

### `{stream_id}.jsonl` (sensor data)

Each line carries a sample plus a `channels` payload. Leaf values must be numeric. Nested dicts and lists are flattened to dot-notation keys (`joints.wrist.0`) at sync time.

```jsonl
{"frame_number":0,"capture_ns":1234567890123456789,"clock_source":"host_monotonic","clock_domain":"mac_studio","channels":{"accel_x":0.12,"accel_y":-9.8,"accel_z":0.05}}
```

### `manifest.json`

Written by `stop()`. Maps every stream to its kind, capabilities, and produced files.

```json
{
  "sdk_version": "0.3.14",
  "host_id": "mac_studio",
  "streams": {
    "mac_webcam": {
      "kind": "video",
      "capabilities": {"provides_audio_track": false, "produces_file": true},
      "status": "completed",
      "frame_count": 900,
      "path": "mac_webcam.mp4"
    },
    "iphone": {
      "kind": "video",
      "capabilities": {"provides_audio_track": false, "produces_file": true},
      "status": "completed",
      "frame_count": 900,
      "path": "iphone.mp4"
    }
  }
}
```

## Development

### Viewer frontend

The browser viewer's React app lives in `src/syncfield/viewer/frontend/`. End users do **not** need Node — published wheels ship the prebuilt SPA in `viewer/static/`. Only contributors editing the viewer need to rebuild.

Requirements:
- **Node ≥ 22** (pinned in `frontend/.nvmrc`; enforced via `package.json` engines). Transitively required by `camera-controls` (via `@react-three/drei`).
- **yarn** as the single package manager. Do not introduce `package-lock.json`.

```bash
cd src/syncfield/viewer/frontend
nvm use                       # picks Node 22 from .nvmrc (optional)
yarn install --frozen-lockfile
yarn build                    # writes ../static/ — what the FastAPI server serves
yarn dev                      # vite dev server on :5173 (HMR)
```

The publish workflow runs the same `yarn install --frozen-lockfile` + `yarn build` before building the Python wheel, so what you see locally matches what ships on PyPI.

## License

Apache-2.0
