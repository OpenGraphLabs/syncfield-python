# Mac + iPhone + Dual OAK

**Four-camera rig running on a single Mac.** The Mac's built-in webcam, an iPhone over Continuity Camera, an OAK-D-Lite, and an OAK-D-S2 — all recorded as one atomic SyncField session with live previews, a countdown, and synchronized start/stop across every device.

This is the next step up from [`iphone_mac_webcam/`](../iphone_mac_webcam/): same session shape, just with two `OakCameraStream` adapters added alongside the two `UVCWebcamStream`s. The whole rig is driven from a single `SessionOrchestrator`.

## What you'll see

When you run `record.py`, the SyncField desktop viewer opens and:

1. **Connect phase** — all four devices open in parallel; stream cards start showing live previews (gradient / face / OAK color sensor) before you press anything.
2. **Record click** — a big `· 3 ·` → `· 2 ·` → `· 1 ·` countdown appears on the session clock panel.
3. **Recording** — every stream begins writing simultaneously, then the start chirp plays (captured into any audio track present), state chip turns red, timer starts ticking.
4. **Stop click** — the stop chirp plays first (so it lands inside the recorded audio), then every stream finalizes its file. The session returns to `CONNECTED` and you can record another episode without re-opening hardware.
5. **Close** — closing the window disconnects every device cleanly.

## Hardware checklist

- [x] **Mac with a working webcam** — built-in FaceTime or any UVC camera at OpenCV index 0
- [x] **iPhone** signed in to the same Apple ID, Continuity Camera enabled, within Bluetooth range of the Mac
- [x] **OAK-D-Lite** plugged into a USB-C port (or hub) on the Mac
- [x] **OAK-D-S2** plugged into a separate USB bus if possible — two OAKs on the same hub share bandwidth and can drop frames at full resolution
- [x] Ideally all four on **wall power**: Continuity can drop mid-session on battery, and OAKs draw enough current to matter on a laptop

## Install

```bash
pip install "syncfield[uvc,oak,audio,viewer]"
```

| Extra | What it's for |
|---|---|
| `uvc` | OpenCV — drives the Mac webcam and the iPhone Continuity Camera through `UVCWebcamStream` |
| `oak` | DepthAI v3 — drives both OAK cameras through `OakCameraStream` |
| `audio` | `sounddevice` — plays the 3-2-1 countdown ticks and start/stop chirps through the MacBook speakers. **Without this extra the session runs in total silence** — no error, just a WARNING in the console that says to install it. |
| `viewer` | DearPyGui + NumPy — the bundled desktop viewer |

If you want depth output from either OAK, the `uvc` extra is also required (the MP4 writer for depth uses OpenCV). Both extras combined are what ship as `syncfield[oak,uvc]`.

### Why you need the `audio` extra

SyncField plays four audible cues during a recording session:

1. **Countdown beep** at each of `3 → 2 → 1` (100 ms C6 tick)
2. **Start chirp** (rising 400 → 2500 Hz sweep, 500 ms) the moment every stream is actually writing
3. **Stop chirp** (falling 2500 → 400 Hz sweep, 500 ms) the moment you press Stop
4. Every one of them is played through the system default output — that's your MacBook speakers unless you've reassigned audio output in macOS

All four go through `sounddevice`. If the `audio` extra isn't installed, `create_default_player()` falls back to `SilentChirpPlayer` and you'll hear nothing. The console will show a WARNING like:

```
WARNING  sounddevice unavailable (No module named 'sounddevice'). The countdown
         ticks and start/stop chirps will be SILENT. Install the audio extra
         to hear them: pip install 'syncfield[audio]'
```

If you see that line and no sound plays, run ``pip install 'syncfield[audio]'`` and rerun ``record.py``.

## Run

Every command below is copy-pastable from the **repo root** (`syncfield-python/`) using `uv run` — no venv, no install step, `uv` resolves the extras against the root `pyproject.toml` and executes the script in a temporary env.

### Step 1: list attached OAKs

First time through, discover the serials of your two OAKs so you can pin each one:

```bash
uv run --extra uvc --extra oak --extra audio --extra viewer \
    python examples/mac_iphone_dual_oak/record.py --list-oak
```

```
Found 2 OAK device(s):
  deviceId=19443010813AF02C00  bus='2.1.4'  product=OAK-D-LITE-AF
      sensors={<CameraBoardSocket.CAM_C: 2>: 'OV7251', <CameraBoardSocket.CAM_A: 0>: 'IMX214', <CameraBoardSocket.CAM_B: 1>: 'OV7251'}
  deviceId=1944301071781C1300  bus='0.1'  product=OAK-D-S2-AF
      sensors={<CameraBoardSocket.CAM_C: 2>: 'OV9282', <CameraBoardSocket.CAM_A: 0>: 'IMX378', <CameraBoardSocket.CAM_B: 1>: 'OV9282'}
```

The `deviceId` field is the persistent DepthAI serial — it never changes across reboots, unlike the USB bus topology (`name`). Copy the serial for each board.

### Step 2: record

```bash
# Default serials match the maintainer's rig; no flags needed
uv run --extra uvc --extra oak --extra audio --extra viewer \
    python examples/mac_iphone_dual_oak/record.py

# Override serials + webcam indices + output directory
uv run --extra uvc --extra oak --extra audio --extra viewer \
    python examples/mac_iphone_dual_oak/record.py \
        --oak-lite 19443010813AF02C00 \
        --oak-d    1944301071781C1300 \
        --webcam-index 0 --iphone-index 1 \
        --output-dir ./my_recording
```

> If you forget `--extra audio`, the recording still works but the 3/2/1 countdown ticks and start/stop chirps play silently and the console prints a WARNING telling you to add it.

### Enabling depth

Both OAKs default to **RGB-only** so the USB-3 bus has headroom for four simultaneous video streams. To enable stereo depth on either:

```bash
# Depth on the OAK-D-S2 only
uv run --extra uvc --extra oak --extra audio --extra viewer \
    python examples/mac_iphone_dual_oak/record.py --oak-d-depth

# Depth on the OAK-D-Lite only
uv run --extra uvc --extra oak --extra audio --extra viewer \
    python examples/mac_iphone_dual_oak/record.py --oak-lite-depth

# Depth on both
uv run --extra uvc --extra oak --extra audio --extra viewer \
    python examples/mac_iphone_dual_oak/record.py --oak-lite-depth --oak-d-depth
```

If you turn on depth for both OAKs on a single USB bus, watch the health events table — drops will appear there if the bandwidth ceiling gets hit.

### Alternative: plain `python` after install

If you'd rather `pip install` into a venv and run the script directly:

```bash
python -m venv .venv && source .venv/bin/activate
pip install "syncfield[uvc,oak,audio,viewer]"
cd examples/mac_iphone_dual_oak
python record.py
```

## Output

```
output/
├── mac_webcam.mp4                  # Mac webcam video
├── mac_webcam.timestamps.jsonl     # Per-frame capture timestamps
├── iphone.mp4                      # iPhone Continuity Camera video
├── iphone.timestamps.jsonl
├── oak_lite.mp4                    # OAK-D-Lite RGB
├── oak_lite.timestamps.jsonl
├── oak_lite.depth.mp4              # (only if --oak-lite-depth)
├── oak_lite.depth.timestamps.jsonl
├── oak_d.mp4                       # OAK-D-S2 RGB
├── oak_d.timestamps.jsonl
├── oak_d.depth.mp4                 # (only if --oak-d-depth)
├── oak_d.depth.timestamps.jsonl
├── sync_point.json                 # Session anchor + chirp metadata
├── manifest.json                   # Stream capabilities + file paths
└── session_log.jsonl               # Crash-safe timeline log
```

## Architecture at a glance

```
┌───────────────────────────────────────────────────────────────────────┐
│  SessionOrchestrator (host_id = mac_studio)                          │
│                                                                       │
│  ┌──────────────────┐ ┌──────────────────┐                           │
│  │ UVCWebcamStream  │ │ UVCWebcamStream  │                           │
│  │ id=mac_webcam    │ │ id=iphone        │                           │
│  │ device_index=0   │ │ device_index=1   │                           │
│  │ → mac_webcam.mp4 │ │ → iphone.mp4     │                           │
│  └──────────────────┘ └──────────────────┘                           │
│                                                                       │
│  ┌──────────────────────┐ ┌──────────────────────┐                   │
│  │ OakCameraStream      │ │ OakCameraStream      │                   │
│  │ id=oak_lite          │ │ id=oak_d             │                   │
│  │ device_id=1944…2C00  │ │ device_id=1944…1C00  │                   │
│  │ → oak_lite.mp4       │ │ → oak_d.mp4          │                   │
│  │  (+ .depth.mp4)      │ │  (+ .depth.mp4)      │                   │
│  └──────────────────────┘ └──────────────────────┘                   │
│                                                                       │
│  Lifecycle: connect → countdown 3/2/1 → start_recording + chirp      │
│             → RECORDING → stop: chirp + stop_recording → CONNECTED   │
└───────────────────────────────────────────────────────────────────────┘
```

Every adapter conforms to the same `Stream` SPI — the orchestrator doesn't know the difference between an OpenCV webcam and a DepthAI pipeline. Adding a fifth stream (a BLE IMU, a tactile sensor, a custom source) is one more `session.add(...)` call.

## Pinning OAKs by serial

The `device_id` kwarg on `OakCameraStream` is the single most important flag in this example. Without it, both `OakCameraStream` instances would call `dai.Device()` with no filter, and DepthAI would hand the "first available device" to whichever pipeline opened first — the second pipeline then finds nothing and raises. Pinning each stream to its persistent DepthAI serial removes the race entirely.

The serials are stable across:

- USB port swaps
- Reboots
- macOS updates
- DepthAI version bumps

They're **not** stable across a physical Myriad-X firmware reflash, which is rare enough to ignore.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `python record.py --list-oak` shows 0 devices | USB enumeration issue | Replug both OAKs, check the lights come on; `depthai` ships with a diagnostic tool you can run |
| Only one OAK appears | USB hub bandwidth limit | Split the two OAKs across separate USB buses; OAK-D-S2 prefers USB 3 SuperSpeed |
| `RuntimeError: X_LINK_ALREADY_OPEN` | Device still held by a previous session | Wait ~10 s for the OAK to recycle, or replug |
| Both previews freeze after ~1 s | Turned depth on for both OAKs on one bus | Run RGB-only (drop the `--*-depth` flags) or move one OAK to a separate bus |
| `ImportError: depthai` | Missing OAK extra | `pip install "syncfield[oak]"` |
| `ImportError: av` | Missing UVC extra | `pip install "syncfield[uvc]"` |
| iPhone card stays blank | Continuity Camera dropped | Wake the iPhone, plug it into power, check System Settings → AirPlay & Handoff |
| Health table shows `drop` events | USB bandwidth saturated | Lower resolution / FPS on one OAK, or split across USB buses |

## Why no chirp timestamps in `sync_point.json`?

None of the four streams declares `provides_audio_track=True`, so the orchestrator logs `"no audio-capable stream registered; chirp injection disabled"` and the `chirp_*_ns` fields stay `null` in the written artifacts. This is the correct behavior for a single-host video-only rig — the sync service falls back to per-stream timestamp alignment, which is exactly what you want when every stream is on the same monotonic clock.

The chirp path re-activates the moment you add an audio-capable stream (e.g., a microphone, or once you move to multi-host mode where a peer Mac captures audio). The recording code stays identical.

## Next steps

1. **Go multi-host.** Run this same script on a second Mac with `FollowerRole` and add a microphone stream so the chirp cross-correlation kicks in. See the [`multi-host`](../../website/docs/sdk/multi-host.md) docs.
2. **Add a BLE IMU.** Register a `BLEImuGenericStream` alongside the cameras — your sensor plot card appears automatically in the viewer.
3. **Replay through the sync service.** The output directory is exactly what `syncfield-app`'s `/api/v1/sync` endpoint expects.
