# iPhone + Mac Webcam

**Shortest end-to-end SyncField recipe.** Two OpenCV-based video streams — the Mac's built-in webcam and an iPhone over Continuity Camera — captured through the desktop viewer and saved to disk.

Use this as the template for your own multi-camera rig. Adding more streams later (OAK-D, BLE IMU, tactile, ...) is a one-line `session.add(...)` change.

## What you'll see

When you run `record.py`, the SyncField desktop viewer pops up with:

- Two stream cards (`mac_webcam`, `iphone`) showing live previews
- A session clock panel
- A big red **Record** button — click it to start the session
- A **Stop** button — click it when you're done
- A running table of any health events either camera emits

Close the viewer window to exit. The output files are on disk regardless of whether you pressed Stop (crash-safe design).

## Hardware checklist

- [x] **Mac with a working webcam** — built-in FaceTime camera or any USB webcam at index 0
- [x] **iPhone** signed in to the same Apple ID as the Mac
- [x] **Continuity Camera enabled** — `System Settings → General → AirPlay & Handoff → Continuity Camera: ON`
- [x] **iPhone within Bluetooth range** of the Mac
- [x] Ideally **both devices on wall power** — Continuity can drop mid-session on battery

## Install

```bash
pip install "syncfield[uvc,audio,viewer]"
```

| Extra | What it's for |
|---|---|
| `uvc` | OpenCV — the `UVCWebcamStream` adapter that drives both cameras |
| `audio` | `sounddevice` — needed by the sync tone / chirp path, even though chirps are skipped in single-host mode |
| `viewer` | `dearpygui` + `numpy` — the bundled desktop viewer |

## Run

```bash
# Default: webcam at index 0, iPhone at index 1
python record.py

# Custom indices, output dir, geometry
python record.py \
    --webcam-index 0 \
    --iphone-index 1 \
    --output-dir ./my_recording \
    --width 1920 --height 1080 --fps 30
```

### Not sure which index is which?

Run the probe first — it prints every openable OpenCV device with its current geometry without starting a recording session:

```bash
python record.py --probe
```

```
Probing OpenCV device indices 0..4 ...

  [0] OK  1280x720 @ 30 fps
  [1] OK  1920x1080 @ 30 fps
  [2] not available
  [3] not available
  [4] not available

Pick the index that matches your Mac webcam and iPhone, then rerun without --probe.
```

If the iPhone doesn't appear, wake it and hold it near the Mac — Continuity Camera activates on-demand.

## Output

```
output/
├── mac_webcam.mp4                  # Mac built-in webcam video
├── mac_webcam.timestamps.jsonl     # Per-frame capture timestamps
├── iphone.mp4                      # iPhone Continuity camera video
├── iphone.timestamps.jsonl
├── sync_point.json                 # Session anchor + chirp metadata
├── manifest.json                   # Stream capabilities + file paths
└── session_log.jsonl               # Crash-safe timeline log
```

The `*.timestamps.jsonl` files plus `sync_point.json` are what the SyncField sync service consumes for post-hoc frame-level alignment. The MP4s are the actual video recordings.

## Architecture at a glance

```
┌─────────────────────────────────────────────────────────────┐
│  SessionOrchestrator (host_id = mac_studio)                 │
│                                                             │
│  ┌──────────────────────┐      ┌──────────────────────┐     │
│  │ UVCWebcamStream      │      │ UVCWebcamStream      │     │
│  │ id = "mac_webcam"    │      │ id = "iphone"        │     │
│  │ device_index = 0     │      │ device_index = 1     │     │
│  │                      │      │                      │     │
│  │ cv2.VideoCapture     │      │ cv2.VideoCapture     │     │
│  │   → MP4 writer       │      │   → MP4 writer       │     │
│  │   → timestamps JSONL │      │   → timestamps JSONL │     │
│  │   → latest_frame     │      │   → latest_frame     │     │
│  └──────────┬───────────┘      └──────────┬───────────┘     │
└─────────────│─────────────────────────────│────────────────┘
              │                             │
              ▼                             ▼
      viewer stream card           viewer stream card
      (live preview)               (live preview)
```

One `SessionOrchestrator`, two `UVCWebcamStream` adapters, one viewer. Every addition to a multi-device rig is just one more `session.add(...)` call.

## Single-host? Multi-host?

This example is **single-host** — one Mac running both cameras. SyncField's multi-host coordination (mDNS discovery, leader/follower roles, chirp-anchored alignment across machines) is handled by a separate set of examples in `examples/multi_host_*`. Both modes share the same `SessionOrchestrator` API — the difference is just which `role` you pass.

The orchestrator is configured with `SyncToneConfig.default()` (chirp enabled), but the chirp is **automatically skipped** here because neither OpenCV camera declares an audio track. You'll see an INFO log line explaining this. The chirp starts firing as soon as you register any stream with `capabilities.provides_audio_track=True` (e.g., an audio-capable OAK camera, a microphone stream, or once you move to multi-host mode).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Viewer opens but both previews are black | Wrong device indices | Run `python record.py --probe` and pass the correct indices |
| `cv2.VideoCapture(1)` fails | iPhone not connected to Continuity Camera | Wake the iPhone, hold it near the Mac, check System Settings |
| iPhone video is 720p instead of 1080p | macOS picked a lower-res stream profile | Try different `--width`/`--height` values; some iPhones cap Continuity at 1280×720 |
| `ImportError: syncfield.viewer requires the 'viewer' extra` | Missing dev dep | `pip install "syncfield[viewer]"` |
| `ImportError: UVCWebcamStream requires opencv-python` | Missing UVC extra | `pip install "syncfield[uvc]"` |
| Recording stops after a few seconds | Continuity Camera dropped | Plug the iPhone into power; keep it awake; minimize Bluetooth contention |
| Frame rate lower than requested | USB bandwidth contention | Lower `--fps`, or plug the webcam into a dedicated USB bus |

## Next steps

Once this recipe works, try:

1. **Add a JSONL sensor log** — register a `JSONLFileStream` alongside the two cameras so sensor data gets the same timestamp treatment. See the `JSONLFileStream` adapter docs.
2. **Swap one camera for an OAK-D** — use `OakCameraStream` to add depth capture.
3. **Go multi-host** — split the two cameras across two Macs and wire them up with `LeaderRole` / `FollowerRole`. See the `sdk/multi-host` docs page.
4. **Add a BLE IMU** — `BLEImuGenericStream` registers like any other stream.

Each is a one-file change on top of this script.
