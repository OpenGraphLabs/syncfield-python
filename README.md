# syncfield-python

Lightweight timestamp capture SDK for [SyncField](https://opengraphlabs.com) multi-stream synchronization.

Captures precise `time.monotonic_ns()` timestamps during multi-camera/sensor recording and produces JSONL files that the SyncField Docker service consumes for frame-level temporal alignment.

## Install

```bash
pip install syncfield
```

**Zero dependencies** — uses only the Python standard library.

## Quick Start

```python
import syncfield as sf

# Start a recording session
session = sf.SyncSession(host_id="rig_01", output_dir="./timestamps")
session.start()

# In your I/O loop — call stamp() immediately AFTER each read()
frame = camera.read()
session.stamp("cam_left", frame_number=i)

data = imu.read()
session.stamp("imu", frame_number=i)

# End the session
session.stop()
```

Output:
```
./timestamps/
  sync_point.json
  cam_left.timestamps.jsonl
  imu.timestamps.jsonl
```

## Best Practices

### Call `stamp()` immediately after I/O read

```python
# GOOD — timestamp reflects when data arrived on the host
data = device.read()
session.stamp("sensor", frame_number=i)  # immediately after read

# BAD — processing delay adds jitter to timestamp
data = device.read()
processed = expensive_transform(data)
session.stamp("sensor", frame_number=i)  # too late!
```

### Use one thread per device

Each device should have its own thread with a tight read loop. `stamp()` is thread-safe.

```python
import threading

def capture_loop(device, stream_id, session):
    i = 0
    while recording:
        data = device.read()
        session.stamp(stream_id, frame_number=i)
        i += 1

t1 = threading.Thread(target=capture_loop, args=(camera, "cam_left", session))
t2 = threading.Thread(target=capture_loop, args=(imu_device, "imu", session))
t1.start()
t2.start()
```

## Integration with SyncField Docker

### Volume-mounted mode

```bash
# Mount timestamps alongside videos
docker run -v ./data:/data -v ./timestamps:/timestamps \
  syncfield-app:latest

# API call
curl -X POST http://localhost:8080/api/v1/sync \
  -H "Content-Type: application/json" \
  -d '{
    "streams": [
      {"path": "/data/cam_left.mp4", "stream_id": "cam_left"},
      {"path": "/data/cam_right.mp4", "stream_id": "cam_right"}
    ],
    "timestamps_dir": "/timestamps",
    "alignment_mode": "timestamp"
  }'
```

The service automatically matches `{stream_id}.timestamps.jsonl` files to video streams.

### File upload mode

```python
import requests

files = [
    ("files", open("cam_left.mp4", "rb")),
    ("files", open("cam_right.mp4", "rb")),
    ("timestamp_files", open("timestamps/cam_left.timestamps.jsonl", "rb")),
    ("timestamp_files", open("timestamps/cam_right.timestamps.jsonl", "rb")),
]
data = {
    "stream_ids": "cam_left,cam_right",
    "primary_id": "cam_left",
    "alignment_mode": "timestamp",
}
resp = requests.post("http://localhost:8080/api/v1/sync/upload", files=files, data=data)
```

## Format Specification

This section defines the output format for implementors in other languages.

### `sync_point.json`

```json
{
  "sdk_version": "0.1.0",
  "monotonic_ns": 1234567890123456789,
  "wall_clock_ns": 1709890101000000000,
  "host_id": "rig_01",
  "timestamp_ms": 1709890101000,
  "iso_datetime": "2024-03-08T12:00:01.000000"
}
```

### `{stream_id}.timestamps.jsonl`

One JSON object per line (no trailing comma, no array wrapper):

```jsonl
{"frame_number":0,"capture_ns":1234567890123456789,"clock_source":"host_monotonic","clock_domain":"rig_01","uncertainty_ns":5000000}
{"frame_number":1,"capture_ns":1234567890156789012,"clock_source":"host_monotonic","clock_domain":"rig_01","uncertainty_ns":5000000}
```

| Field | Type | Description |
|-------|------|-------------|
| `frame_number` | int | 0-based sequential index |
| `capture_ns` | int | Monotonic nanoseconds at data arrival |
| `clock_source` | string | Always `"host_monotonic"` for SDK output |
| `clock_domain` | string | Must match `host_id` — identifies the clock |
| `uncertainty_ns` | int | Timing uncertainty (default: 5000000 = 5ms) |

**Key rules:**
- `capture_ns` must be monotonically non-decreasing within each stream
- `clock_domain` must be identical across all streams on the same host
- File name must be `{stream_id}.timestamps.jsonl` for auto-matching

### Sensor Data JSONL (`{sensor_id}.jsonl`)

Sensor data files are self-contained — timestamps and channel values in one file.

**Minimal format** (recommended):
```jsonl
{"capture_ns":1234567890123456789,"channels":{"accel_x":0.12,"accel_y":-9.8,"accel_z":0.05}}
{"capture_ns":1234567890133456789,"channels":{"accel_x":0.13,"accel_y":-9.7,"accel_z":0.06}}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `capture_ns` | int | Yes | Monotonic nanoseconds (same clock as video timestamps) |
| `channels` | object | Yes | Sensor values as key-value pairs |
| `frame_number` | int | No | Auto-assigned from line order if missing |
| `clock_domain` | string | No | Auto-filled from video streams if missing |
| `uncertainty_ns` | int | No | Defaults to 5000000 (5ms) |

**Capture pattern:**
```python
data = imu.read()
ts = session.stamp("imu", frame_number=i)  # SDK captures timestamp
my_file.write(json.dumps({"capture_ns": ts, "channels": parse_imu(data)}) + "\n")
```

## License

Apache-2.0
