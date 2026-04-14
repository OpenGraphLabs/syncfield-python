# Meta Quest 3 Camera Adapter — Hardware QA Checklist

Run this against a physical Quest 3 + Macbook before merging any change to
the `MetaQuestCameraStream` adapter or its Unity counterparts.

## Pre-flight

- [ ] Quest 3 running Horizon OS v74+ with the SyncField companion Unity app built and installed
- [ ] Camera + microphone permissions granted on Quest
- [ ] Quest and Macbook on the same WiFi 5/6 network
- [ ] `uv run pytest tests/` passes on Mac before hardware test

## Feasibility probe (Unity-side, one-time per Unity change)

- [ ] Unity feasibility scene sustains 30 fps on both cameras simultaneously for 3 minutes
- [ ] Hand tracking packets continue to arrive at 72 Hz during camera capture (check UDPTrackingSender logs)
- [ ] H.264 hardware encoder reports no dropped frames (MediaCodec logs)

## Adapter integration

- [ ] `MetaQuestCameraStream(...)` connects successfully after discovery finds the Quest
- [ ] Viewer shows both preview frames within 2 s of `connect()`
- [ ] Preview stays live through a 3-min idle period
- [ ] `start_recording()` returns without errors
- [ ] `SampleEvent`s arrive at ~30 Hz during recording (left-eye driven)
- [ ] `stop_recording()` completes within 30 s of stop for a 3-min session
- [ ] Four output files exist with the expected names and non-zero sizes
- [ ] MP4 files play back in VLC (visual smoke test)
- [ ] Per-eye `.timestamps.jsonl` lines all have `clock_domain == "remote_quest3"` and monotonic `capture_ns`

## Error paths

- [ ] Turning WiFi off mid-recording surfaces a health event within ~2 s
- [ ] Quest running out of storage surfaces a warning via `/status`
- [ ] Killing the Unity app mid-session causes `stop_recording()` to return `status="failed"` with a descriptive error
