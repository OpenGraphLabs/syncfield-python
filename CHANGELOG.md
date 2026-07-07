# Changelog

## 0.6.0

- **Reliable stream health + supervision.** New `syncfield.supervision` module with a per-stream health state machine (`StreamConnectionState`: idle → connecting → connected → stalled → reconnecting → failed/disconnected) that emits **transition-only** events — replacing the previous noisy, effectively-unused health signal. Exposed as an immutable `StreamStatus` snapshot.
- **Public per-stream status API** on `SessionOrchestrator`: `stream_status(id)`, `stream_statuses()`, and `on_stream_status(cb)`. Consumers no longer reach into private `_stream_states` / `_stream_errors`.
- **Bounded reconnect.** Opt-in `ReconnectPolicy` (disabled by default → zero behaviour change) drives automatic, backed-off reconnection of dropped streams. Preview/pre-flight drops always recover; mid-recording reconnect is gated behind the new `StreamCapabilities.supports_recording_reconnect` (default `False`) so video adapters can't corrupt a bundle. `Stream.reconnect()` added to the SPI with a safe `disconnect()`+`connect()` default.
- **Capture-loop death is now first-class.** A dying capture thread (the highest-signal reliability event) drives the supervisor's state machine and reconnect instead of being dropped from the incident layer.
- **De-noised the default health surface.** The soft-threshold quality detectors `fps-drop` and `jitter`, and the zero-fed (dead) `backpressure` detector, are no longer installed by default; the reliable `stream-stall`, `no-data`, `startup-failure`, and adapter/crash detectors remain. The detector classes stay importable for explicit opt-in.

## 0.5.0

- Promoted the full OAK-D Pro composite adapter (3 cameras + IMU + hardware FSYNC + EEPROM calibration) into the SDK as the canonical `OakCameraStream`; the prior RGB+optional-depth adapter is preserved as `OakRgbDepthStream`.
- Rewrote the OGLO tactile adapter for the latest firmware (`schema_ver 5`, `packed12_v5`): 80 taxels (5×4×4) at 12-bit plus a per-sample 6-axis wrist IMU and device-clock timestamps, all described by the config manifest read + hard-validated at connect. The wrist IMU is split into a derived substream self-written to `{id}.imu.jsonl`; taxel channels are labelled `<finger>_<row>_<col>` (raw 0–4095). No runtime command writes. Removed the legacy 5-FSR protocol.

## 0.4.0

- Added `SampleEvent.device_ns` as the canonical per-sample device-clock timestamp API.
- Writers now persist `device_ns` as top-level `device_timestamp_ns` for both stream timestamp JSONL and sensor JSONL rows.
- Migrated OAK, OGLO tactile, BLE IMU, push sensor, and polling sensor paths to preserve host `capture_ns` semantics while exposing device-clock timestamps for sync refinement.
- Restored partial-connect behavior so one failed device does not block healthy streams from recording, while failed streams are cleaned up immediately.
