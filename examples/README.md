# SyncField SDK Examples

Runnable end-to-end recipes showing real SDK setups. Each subdirectory is a self-contained example with a `README.md` explaining the hardware + setup, a `record.py` script you can run directly, and a description of what output it produces.

Start with the simplest example that matches hardware you actually have, then scale up by swapping or adding one adapter at a time.

## Catalog

| Example | Hardware | What it shows |
|---|---|---|
| [`iphone_mac_webcam/`](./iphone_mac_webcam/) | Mac built-in webcam + iPhone (Continuity Camera) | Shortest end-to-end recipe: two OpenCV video streams through `UVCWebcamStream`, live preview in the desktop viewer, MP4 + timestamps written to disk |

More recipes will be added as the rigs they target come online. Expected next:

- **`oak_plus_webcam/`** — OAK-D Pro depth camera + Mac webcam (add depth to the dual-camera setup)
- **`iphone_imu/`** — iPhone + BLE IMU (`BLEImuGenericStream`) showing mixed video + sensor streams
- **`tactile_rig/`** — webcam + tactile sensor via `OgloTactileStream` showing custom-adapter integration
- **`multi_host_pair/`** — two Macs on the same WiFi recording together with `LeaderRole` / `FollowerRole`

## How to run any example

Every example follows the same shape:

```bash
cd examples/<name>
pip install "syncfield[uvc,audio,viewer]"   # extras vary — see the example's README
python record.py                             # blocking, opens the viewer
```

Inside the viewer, click **Record** to start the session, **Stop** to finish, and close the window to exit. Output files land in `./output/` by default; every example accepts `--output-dir` if you want a different location.

## Architecture shared by every example

Whatever the hardware, every recipe builds the same three-step pipeline:

```
1. Construct one SessionOrchestrator
      ↓
2. Register one Stream per capture source (session.add(...))
      ↓
3. Launch the viewer — it drives start() / stop() from the UI buttons
```

Swapping or adding streams is always a one-line change, which is the whole point of the `Stream` SPI — the orchestrator doesn't know or care whether a stream is a webcam, a depth camera, or a tactile sensor.

See the [**Python SDK docs**](https://opengraphlabs.com/sdk/python) for the full API reference and the [**Concepts**](https://opengraphlabs.com/concepts) page for how these recipes fit into the larger capture-then-sync workflow.

## Adding your own example

New examples go into their own subdirectory:

```
examples/your_recipe/
├── README.md    # Hardware checklist, install, run, output, troubleshooting
└── record.py    # One runnable script — keep it under ~200 lines
```

Keep each `record.py` self-contained (no shared helpers across examples) so readers can copy-paste one file and have it work. Prefer clarity over cleverness: comments that explain *why*, not *what*.
