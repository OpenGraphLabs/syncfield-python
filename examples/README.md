# Examples

## Setup

```bash
git clone https://github.com/OpenGraphLabs/syncfield-python.git
cd syncfield-python
uv sync --all-extras
```

Or from PyPI:

```bash
pip install syncfield
```

The default install covers UVC cameras, the viewer, and the chirp/countdown audio player. Extras (`ble`, `camera`, `oak`, `multihost`) are listed per-example below.

## Catalog

| Example | Command | Hardware | Extras |
|---------|---------|----------|--------|
| Mac webcam + iPhone | `uv run python examples/iphone_mac_webcam/record.py` | Mac + iPhone (Continuity) | — |
| 4 cameras (Mac+iPhone+OAK×2) | `uv run python examples/mac_iphone_dual_oak/record.py` | + 2× OAK-D | `oak` |
| Quest 3 full rig | `uv run python examples/full_rig/record.py` | Quest 3 + 4 IMUs + Go3S + glove | `ble`, `camera` |
| Quest 3 only | `uv run python examples/meta_quest/record.py` | Quest 3 | `camera` |
| Insta360 Go3S | `uv run python examples/insta360_go3s/record.py` | Go3S | `camera` |
| Polling sensor | `uv run python examples/generic_sensor_demo/polling_serial.py` | Serial sensor | — |
| Async push sensor | `uv run python examples/generic_sensor_demo/push_async.py` | None (fake) | — |
| Multi-host leader | `uv run python examples/multihost_lab/leader.py` | Mac + iPhone | `multihost` |
| Multi-host follower | `uv run python examples/multihost_lab/follower.py` | Mac + iPhone | `multihost` |

## Multi-host

Same LAN, two or more MacBooks:

```bash
# Leader
uv run python examples/multihost_lab/leader.py

# Every follower
uv run python examples/multihost_lab/follower.py
```

For more than one follower, edit each follower's `host_id="mac_b"` to a unique value.

## Output layout

- Single-host: `examples/<example>/output/ep_<timestamp>/`
- Multi-host (collected on leader): `examples/multihost_lab/output/<session_id>/<leader_episode>/<host>.<filename>`

## See also

- Per-example details: each folder's `README.md`
- Hardware auto-discovery: `uv run python -m syncfield.discovery`
