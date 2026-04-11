# Full Rig — Mac + iPhone + Dual OAK + OGLO Glove

**Five streams on one host.** Mac built-in webcam, iPhone over Continuity Camera, OAK-D-Lite, OAK-D-S2, and an OGLO tactile glove over BLE — all recorded as one atomic SyncField session.

This is the next step up from [`mac_iphone_dual_oak/`](../mac_iphone_dual_oak/): same four video streams, plus a 100 Hz tactile sensor card that renders a live 5-finger FSR plot in the viewer card row.

## Hardware checklist

- [x] Mac with a working webcam
- [x] iPhone with Continuity Camera enabled
- [x] OAK-D-Lite connected via USB
- [x] OAK-D-S2 connected via USB (different bus if possible)
- [x] OGLO tactile glove powered on and advertising over BLE within ~5 m of the Mac
- [x] Bluetooth enabled on the Mac

## Install

```bash
pip install "syncfield[uvc,oak,ble,audio,viewer]"
```

| Extra | What it's for |
|---|---|
| `uvc` | OpenCV — Mac webcam + iPhone Continuity Camera |
| `oak` | DepthAI v3 — both OAK cameras |
| `ble` | `bleak` — BLE scan + notify subscription for the OGLO glove |
| `audio` | `sounddevice` — 3/2/1 countdown ticks + start/stop sync chirps |
| `viewer` | DearPyGui + NumPy — the bundled desktop viewer |

## Run

From the repo root:

```bash
# Default serials + the currently paired OGLO address on the maintainer's rig
uv run --extra uvc --extra oak --extra ble --extra audio --extra viewer \
    python examples/full_rig/record.py

# Override any identifier
uv run --extra uvc --extra oak --extra ble --extra audio --extra viewer \
    python examples/full_rig/record.py \
        --oak-lite    19443010813AF02C00 \
        --oak-d       1944301071781C1300 \
        --oglo-address C1718989-5A77-F3EB-B00A-01A758D99D54 \
        --oglo-hand   right \
        --output-dir  ./my_recording
```

Drop `--oglo-address` to fall back to a BLE name-substring scan for `"oglo"` — slower (scans for ~10 s at session start) but works on any Mac even if the CoreBluetooth address changes after re-pairing.

## Finding the OGLO BLE address on a new Mac

On macOS the BLE "address" returned by `bleak` is a per-host CoreBluetooth UUID — stable across reboots but different on every Mac. Discover yours with:

```bash
uv run --extra ble python -c "
import asyncio, bleak
SERVICE = '4652535f-424c-4500-0000-000000000001'
async def main():
    results = await bleak.BleakScanner.discover(timeout=10, return_adv=True)
    for addr, (d, ad) in results.items():
        if SERVICE.lower() in [s.lower() for s in (ad.service_uuids or [])]:
            print(f'OGLO → address={addr}  local_name={ad.local_name!r}')
asyncio.run(main())
"
```

Copy the printed address into the `--oglo-address` flag (or into `DEFAULT_OGLO_ADDRESS` at the top of `record.py`).

## Output

```
output/
├── mac_webcam.mp4              mac_webcam.timestamps.jsonl
├── iphone.mp4                  iphone.timestamps.jsonl
├── oak_lite.mp4                oak_lite.timestamps.jsonl
├── oak_d.mp4                   oak_d.timestamps.jsonl
├── oglo.timestamps.jsonl       (no .mp4 — OGLO is a sensor stream)
├── sync_point.json
├── manifest.json
└── session_log.jsonl
```

The OGLO stream is tagged `kind="sensor"` with `produces_file=False`, so you get one JSONL per tactile sample (thumb/index/middle/ring/pinky + device timestamp in nanoseconds) instead of a video file. In the viewer it renders as a multi-series line plot card alongside the four camera cards.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `oglo` card stays blank | Glove is off, out of range, or claimed by another app | Power-cycle the glove; make sure the iOS egonaut app isn't connected to it at the same time |
| Session hangs for 10 s at connect | You dropped `--oglo-address` and it's running the fallback name scan | Normal — the scan runs once at connect and the session proceeds after it |
| `OGLO connection failed: no peripheral matched` | Name scan couldn't find the glove | Use the one-liner above to discover the address, then pass `--oglo-address` |
| `RuntimeError: No OAK devices found` | Old viewer still holds the USB handles | Close the previous viewer window, wait ~5 s, retry |

See [`mac_iphone_dual_oak/README.md`](../mac_iphone_dual_oak/README.md) for OAK-specific troubleshooting and [`iphone_mac_webcam/README.md`](../iphone_mac_webcam/README.md) for webcam troubleshooting.
