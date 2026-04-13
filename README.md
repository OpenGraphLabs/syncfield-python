# syncfield-python

Lightweight Python SDK for [SyncField](https://opengraphlabs.com) multi-stream synchronization. Captures precise timestamps during multi-camera and sensor recording and produces JSONL files that the SyncField Docker service consumes for frame-level temporal alignment.

## Install

```bash
pip install syncfield
```

**Zero dependencies** -- uses only the Python standard library.

## Quick Start

### Video Streams

Use `stamp()` to capture timestamps and `link()` to associate the saved video file with the stream.

```python
import syncfield as sf

session = sf.SyncSession(host_id="rig_01", output_dir="./sync_data")
session.start()

for i in range(num_frames):
    frame = camera.read()
    session.stamp("cam_left", frame_number=i)
    save_frame_to_video(frame, "cam_left.mp4")

session.link("cam_left", "/data/cam_left.mp4")
session.stop()
```

Output:
```
./sync_data/
  sync_point.json
  cam_left.timestamps.jsonl
  manifest.json
```

### Sensor Streams

Use `record()` to capture timestamps and sensor data in one call. This writes both a `.timestamps.jsonl` file (for alignment) and a `.jsonl` file (sensor channel values).

```python
import syncfield as sf

session = sf.SyncSession(host_id="rig_01", output_dir="./sync_data")
session.start()

for i in range(num_samples):
    data = imu.read()
    session.record("imu", frame_number=i, channels={
        "accel_x": data.ax,
        "accel_y": data.ay,
        "accel_z": data.az,
    })

session.stop()
```

Output:
```
./sync_data/
  sync_point.json
  imu.timestamps.jsonl
  imu.jsonl
  manifest.json
```

### Complex Sensor Data

Sensors like hand trackers, tactile arrays, and robot joints produce nested data. The SDK handles these natively — leaf values must be numeric (float or int).

```python
# Hand tracker — nested joint positions and gestures
session.record("hand_tracker", frame_number=i, channels={
    "joints": {
        "wrist": [0.1, 0.2, 0.3],
        "thumb_tip": [0.4, 0.5, 0.6],
        "index_tip": [0.7, 0.8, 0.9],
    },
    "gestures": {"pinch": 0.95, "fist": 0.02},
    "finger_angles": [12.5, 45.0, 30.0, 15.0, 5.0],
})

# Tactile grid — 2D pressure array
session.record("tactile", frame_number=i, channels={
    "pressure_grid": [[0.1, 0.2, 0.3, 0.4],
                       [0.5, 0.6, 0.7, 0.8]],
    "total_force": 12.5,
})

# Robot arm — joint states
session.record("robot_arm", frame_number=i, channels={
    "joint_positions": [0.0, -1.57, 0.0, -1.57, 0.0, 0.0],
    "joint_velocities": [0.01, -0.02, 0.0, 0.01, 0.0, 0.0],
    "gripper": {"width": 0.04, "force": 5.2},
})
```

SyncField automatically flattens nested channels for aggregation using dot-notation keys (e.g., `joints.wrist.0`, `gripper.width`).

### Multi-Stream Example

A complete example with 2 cameras and 1 IMU, each in its own thread.

```python
import threading
import syncfield as sf

session = sf.SyncSession(host_id="rig_01", output_dir="./sync_data")
session.start()

recording = True

def camera_loop(cam, stream_id, video_path):
    i = 0
    while recording:
        frame = cam.read()
        session.stamp(stream_id, frame_number=i)
        save_frame(frame, video_path)
        i += 1
    session.link(stream_id, video_path)

def imu_loop(imu, stream_id):
    i = 0
    while recording:
        data = imu.read()
        session.record(stream_id, frame_number=i, channels={
            "accel_x": data.ax, "accel_y": data.ay, "accel_z": data.az,
            "gyro_x": data.gx, "gyro_y": data.gy, "gyro_z": data.gz,
        })
        i += 1

threads = [
    threading.Thread(target=camera_loop, args=(cam_left, "cam_left", "/data/cam_left.mp4")),
    threading.Thread(target=camera_loop, args=(cam_right, "cam_right", "/data/cam_right.mp4")),
    threading.Thread(target=imu_loop, args=(imu_device, "imu")),
]

for t in threads:
    t.start()

# ... record for desired duration ...
recording = False

for t in threads:
    t.join()

counts = session.stop()
# counts == {"cam_left": 900, "cam_right": 900, "imu": 9000}
```

Output directory:
```
./sync_data/
  sync_point.json
  cam_left.timestamps.jsonl
  cam_right.timestamps.jsonl
  imu.timestamps.jsonl
  imu.jsonl
  manifest.json
```

## Multi-host research lab

Run a coordinated capture session across 2–N MacBooks with zero manual
coordination. See [`examples/multihost_lab/`](examples/multihost_lab/)
for the full recipe.

Short version (every host, same LAN, `pip install "syncfield[multihost,uvc,audio]"`):

```bash
# Leader MacBook
python examples/multihost_lab/leader.py

# Every other MacBook
python examples/multihost_lab/follower.py
```

The leader plays a rising audio chirp at start and a falling chirp at
stop; every host's microphone captures both, and the sync service
cross-correlates them post-hoc for sub-5ms inter-host alignment. The
leader auto-pushes session config to each follower over a local HTTP
control plane, and after `stop()` pulls every follower's files into
one canonical tree via `session.collect_from_followers()`.

## Best Practices

### Call `stamp()`/`record()` immediately after I/O read

The timestamp should reflect when data arrived on the host, not when processing finished.

```python
# GOOD -- timestamp reflects when data arrived on the host
data = device.read()
session.stamp("sensor", frame_number=i)  # immediately after read

# BAD -- processing delay adds jitter to timestamp
data = device.read()
processed = expensive_transform(data)
session.stamp("sensor", frame_number=i)  # too late!
```

### Use one thread per device

Each device should have its own thread with a tight read loop. Both `stamp()` and `record()` are thread-safe.

```python
import threading

def camera_thread(cam, stream_id, session):
    i = 0
    while recording:
        frame = cam.read()
        session.stamp(stream_id, frame_number=i)
        i += 1

def sensor_thread(imu, stream_id, session):
    i = 0
    while recording:
        data = imu.read()
        session.record(stream_id, frame_number=i, channels={
            "accel_x": data.ax, "accel_y": data.ay, "accel_z": data.az,
        })
        i += 1

t1 = threading.Thread(target=camera_thread, args=(camera, "cam_left", session))
t2 = threading.Thread(target=sensor_thread, args=(imu_device, "imu", session))
t1.start()
t2.start()
```

## Integration with SyncField Docker

### Using `manifest.json` (recommended)

After `stop()`, the SDK writes a `manifest.json` that maps all streams to their files. Use it to construct the API request body programmatically.

```python
import json
import requests

# Read the manifest produced by the SDK
with open("./sync_data/manifest.json") as f:
    manifest = json.load(f)

host_id = manifest["host_id"]

# Build the streams list from manifest entries
streams = []
for stream_id, info in manifest["streams"].items():
    stream_entry = {"stream_id": stream_id}

    if "path" in info:
        stream_entry["path"] = info["path"]

    if info.get("type") == "sensor":
        stream_entry["stream_type"] = "sensor"

    streams.append(stream_entry)

# Mark the first video stream as primary
for s in streams:
    entry = manifest["streams"][s["stream_id"]]
    if entry.get("type") == "video":
        s["is_primary"] = True
        break

# Submit to SyncField Docker
resp = requests.post("http://localhost:8080/api/v1/sync", json={
    "hosts": [
        {
            "host_id": host_id,
            "streams": streams,
        }
    ],
    "timestamps_dir": "/timestamps",
})
print(resp.json())  # {"job_id": "a1b2c3d4"}
```

### Volume-mounted mode

Mount your data and timestamp directories into the container and call the API directly.

```bash
docker run -v ./data:/data -v ./sync_data:/timestamps \
  syncfield-app:latest
```

```bash
curl -X POST http://localhost:8080/api/v1/sync \
  -H "Content-Type: application/json" \
  -d '{
    "hosts": [
      {
        "host_id": "rig_01",
        "streams": [
          {"path": "/data/cam_left.mp4", "stream_id": "cam_left", "is_primary": true},
          {"path": "/data/cam_right.mp4", "stream_id": "cam_right"},
          {"stream_id": "imu", "stream_type": "sensor"}
        ]
      }
    ],
    "timestamps_dir": "/timestamps"
  }'
```

The service automatically matches `{stream_id}.timestamps.jsonl` and `{stream_id}.jsonl` files to streams using the `timestamps_dir` path.

### File upload mode

Upload files directly without volume mounts. Use `host_ids` to group streams by host.

```python
import requests

files = [
    ("files", open("cam_left.mp4", "rb")),
    ("files", open("cam_right.mp4", "rb")),
    ("timestamp_files", open("sync_data/cam_left.timestamps.jsonl", "rb")),
    ("timestamp_files", open("sync_data/cam_right.timestamps.jsonl", "rb")),
]
data = {
    "stream_ids": "cam_left,cam_right",
    "host_ids": "rig_01,rig_01",
    "primary_id": "cam_left",
}
resp = requests.post("http://localhost:8080/api/v1/sync/upload", files=files, data=data)
print(resp.json())  # {"job_id": "a1b2c3d4"}
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
| `clock_domain` | string | Must match `host_id` -- identifies the clock |
| `uncertainty_ns` | int | Timing uncertainty (default: 5000000 = 5ms) |

**Key rules:**
- `capture_ns` must be monotonically non-decreasing within each stream
- `clock_domain` must be identical across all streams on the same host
- File name must be `{stream_id}.timestamps.jsonl` for auto-matching

### `{stream_id}.jsonl` (Sensor Data)

One JSON object per line, combining timestamp and channel values:

```jsonl
{"frame_number":0,"capture_ns":1234567890123456789,"clock_source":"host_monotonic","clock_domain":"rig_01","uncertainty_ns":5000000,"channels":{"accel_x":0.12,"accel_y":-9.8,"accel_z":0.05}}
{"frame_number":1,"capture_ns":1234567890133456789,"clock_source":"host_monotonic","clock_domain":"rig_01","uncertainty_ns":5000000,"channels":{"accel_x":0.13,"accel_y":-9.7,"accel_z":0.06}}
```

| Field | Type | Description |
|-------|------|-------------|
| `frame_number` | int | 0-based sequential index |
| `capture_ns` | int | Monotonic nanoseconds at data arrival (same clock as video timestamps) |
| `clock_source` | string | Origin of the timestamp (always `"host_monotonic"` for SDK) |
| `clock_domain` | string | Host identifier -- must match across all streams on the same host |
| `uncertainty_ns` | int | Timing uncertainty (default: 5000000 = 5ms) |
| `channels` | object | Sensor values as key-value pairs (e.g. `{"accel_x": 0.12}`) |

### `manifest.json`

Written by `stop()`. Maps all streams in the session to their output files.

```json
{
  "sdk_version": "0.1.0",
  "host_id": "rig_01",
  "streams": {
    "cam_left": {
      "type": "video",
      "timestamps_path": "cam_left.timestamps.jsonl",
      "frame_count": 900,
      "path": "/data/cam_left.mp4"
    },
    "cam_right": {
      "type": "video",
      "timestamps_path": "cam_right.timestamps.jsonl",
      "frame_count": 900,
      "path": "/data/cam_right.mp4"
    },
    "imu": {
      "type": "sensor",
      "sensor_path": "imu.jsonl",
      "timestamps_path": "imu.timestamps.jsonl",
      "frame_count": 9000
    }
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `sdk_version` | string | SDK version that produced this file |
| `host_id` | string | Host identifier for this recording session |
| `streams` | object | Map of `stream_id` to stream metadata |
| `streams.*.type` | string | `"video"` or `"sensor"` |
| `streams.*.timestamps_path` | string | Relative path to the timestamps JSONL file |
| `streams.*.frame_count` | int | Number of frames/samples recorded |
| `streams.*.path` | string | (video only) Path set via `link()` |
| `streams.*.sensor_path` | string | (sensor only) Relative path to the sensor data JSONL file |

## License

Apache-2.0
