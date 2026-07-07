# Changelog

## 0.5.0

- Promoted the full OAK-D Pro composite adapter (3 cameras + IMU + hardware FSYNC + EEPROM calibration) into the SDK as the canonical `OakCameraStream`; the prior RGB+optional-depth adapter is preserved as `OakRgbDepthStream`.
- Rewrote the OGLO tactile adapter for the latest firmware (`schema_ver 5`, `packed12_v5`): 80 taxels (5×4×4) at 12-bit plus a per-sample 6-axis wrist IMU and device-clock timestamps, all described by the config manifest read + hard-validated at connect. The wrist IMU is split into a derived substream self-written to `{id}.imu.jsonl`; taxel channels are labelled `<finger>_<row>_<col>` (raw 0–4095). No runtime command writes. Removed the legacy 5-FSR protocol.

## 0.4.0

- Added `SampleEvent.device_ns` as the canonical per-sample device-clock timestamp API.
- Writers now persist `device_ns` as top-level `device_timestamp_ns` for both stream timestamp JSONL and sensor JSONL rows.
- Migrated OAK, OGLO tactile, BLE IMU, push sensor, and polling sensor paths to preserve host `capture_ns` semantics while exposing device-clock timestamps for sync refinement.
- Restored partial-connect behavior so one failed device does not block healthy streams from recording, while failed streams are cleaned up immediately.
