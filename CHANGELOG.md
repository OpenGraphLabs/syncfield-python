# Changelog

## 0.4.0

- Added `SampleEvent.device_ns` as the canonical per-sample device-clock timestamp API.
- Writers now persist `device_ns` as top-level `device_timestamp_ns` for both stream timestamp JSONL and sensor JSONL rows.
- Migrated OAK, OGLO tactile, BLE IMU, push sensor, and polling sensor paths to preserve host `capture_ns` semantics while exposing device-clock timestamps for sync refinement.
- Restored partial-connect behavior so one failed device does not block healthy streams from recording, while failed streams are cleaned up immediately.
