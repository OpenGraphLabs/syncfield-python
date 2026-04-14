# Insta360 Go3S example

Records via BLE trigger; downloads files in a background WiFi aggregation
job after the session ends.

## One-time setup

1. **Pair the Go3S** with the laptop using its BLE name (e.g. via the system
   Bluetooth pane). After pairing, the BLE address persists.
2. **Discover the BLE address**:
   ```
   uv run python -c "import asyncio; from bleak import BleakScanner; \
     print(asyncio.run(BleakScanner.discover()))"
   ```
3. **macOS only**: the first WiFi switch will request Location permission
   (required by `networksetup`). Grant once.

## Run

```
uv run python examples/insta360_go3s/record.py \
    --address AA:BB:CC:DD:EE:FF \
    --output ./go3s_output \
    --duration 10
```

After `stop`, the SDK reports `pending_aggregation` and a background worker
switches the host WiFi to the camera AP, downloads the video file, and
restores the previous network. Episode dir contents:

```
go3s_output/
├── overhead.mp4              ← downloaded
├── aggregation.json          ← per-episode atomic state
├── manifest.json             ← session metadata
└── ...
```

## Multihost note

If you use a `LeaderRole` or `FollowerRole`, the adapter automatically
downgrades the policy to `on_demand` so aggregation does not break lab
WiFi (mDNS) during the session. Trigger aggregation explicitly from the
viewer's "Aggregate now" button after recording wraps.

## Limitations (v1)

- No live preview (the camera does not expose one over the BLE/OSC path).
- No Windows WiFi switching (`NotImplementedError`); BLE-only flows still work.
- Per-camera resolution/fps uses the camera's own UI setting.
- Aggregation across multiple Go3S devices is sequential per episode.
