# SyncField SDK Examples

Runnable end-to-end recipes showing real SDK setups. Each subdirectory is a self-contained example with a `README.md` explaining the hardware + setup, a `record.py` script you can run directly, and a description of what output it produces.

Start with the simplest example that matches hardware you actually have, then scale up by swapping or adding one adapter at a time.

## Catalog

| Example | Hardware | What it shows |
|---|---|---|
| [`iphone_mac_webcam/`](./iphone_mac_webcam/) | Mac built-in webcam + iPhone (Continuity Camera) | Shortest end-to-end recipe: two OpenCV video streams through `UVCWebcamStream`, live preview in the desktop viewer, MP4 + timestamps written to disk |
| [`mac_iphone_dual_oak/`](./mac_iphone_dual_oak/) | Mac webcam + iPhone + OAK-D-Lite + OAK-D-S2 | Four video streams on one host: two `UVCWebcamStream`s and two `OakCameraStream`s, each OAK pinned to its DepthAI serial. Optional stereo depth flags on either OAK. |
| [`full_rig/`](./full_rig/) | Mac webcam + iPhone + OAK-D-Lite + OAK-D-S2 + OGLO glove (BLE) | Mixes four video streams with a 100 Hz tactile sensor stream: same four cameras as above plus an `OgloTactileStream` that renders a 5-finger FSR plot card in the viewer. Shows how video + sensor streams share one atomic session. |

More recipes will be added as the rigs they target come online. Expected next:

- **`iphone_imu/`** — iPhone + BLE IMU (`BLEImuGenericStream`) showing mixed video + sensor streams
- **`tactile_rig/`** — webcam + tactile sensor via `OgloTactileStream` showing custom-adapter integration
- **`multi_host_pair/`** — two Macs on the same WiFi recording together with `LeaderRole` / `FollowerRole`

## Run any example (`uv run`)

Every example is a plain Python script inside this repo, so the easiest way to run one is with `uv run` from the repo root — no virtualenv setup, no `pip install` step. `uv` resolves the extras you pass with `--extra` against the root `pyproject.toml` and executes the script in a temporary env:

```bash
# iphone_mac_webcam — Mac webcam + iPhone Continuity Camera
uv run --extra uvc --extra audio --extra viewer \
    python examples/iphone_mac_webcam/record.py

# mac_iphone_dual_oak — Mac webcam + iPhone + OAK-D-Lite + OAK-D-S2
uv run --extra uvc --extra oak --extra audio --extra viewer \
    python examples/mac_iphone_dual_oak/record.py

# full_rig — dual_oak + OGLO tactile glove over BLE
uv run --extra uvc --extra oak --extra ble --extra audio --extra viewer \
    python examples/full_rig/record.py
```

The first run for each extra set downloads the wheels into the uv cache (~10–30 s); every subsequent run is instant.

> **Why `audio` is always there.** SyncField plays a 3/2/1 countdown tick and start/stop sync chirps through `sounddevice`. Without the `audio` extra installed the session runs in total silence and the console prints a WARNING telling you to add it. Every example in this directory includes `audio` in its recommended extras for that reason.

### Common flags

Every `record.py` accepts at least:

```bash
--output-dir ./my_recording   # where to write session artifacts (default ./output)
```

Individual examples have their own extra flags — see the per-example README.

### Alternative: install once, then `python`

If you'd rather install the package into a persistent environment and run `python record.py` directly (closer to how end-users would ship it), use either of:

```bash
# Plain pip + venv
python -m venv .venv && source .venv/bin/activate
pip install "syncfield[uvc,oak,audio,viewer]"
python examples/mac_iphone_dual_oak/record.py

# uv sync
uv sync --extra uvc --extra oak --extra audio --extra viewer
uv run python examples/mac_iphone_dual_oak/record.py
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
