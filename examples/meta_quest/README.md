# Meta Quest 3 — egocentric camera + head/hand pose

Captures four synchronised streams from a single Quest 3 headset:

| Stream | Data | Rate | Transport |
|---|---|---|---|
| `quest_tracking` | 26-joint hand skeleton × 2, head pose | 72 Hz | UDP :14043 |
| `quest_cam` (left + right) | 720p stereo passthrough MP4 | 30 fps | HTTP :14045 |

All samples land in the `remote_quest3` clock domain with a 10 ms
uncertainty budget so they align with on-host sensors (Mac webcam,
IMU, etc.) via the standard SyncField sync pipeline.

---

## Prerequisites

### Quest 3 side

1. Install the **SyncField Quest Sender** companion app on the
   headset. Source in
   `opengraph-studio/unity/SyncFieldQuest3Sender/` —
   build with `scripts/build_and_deploy.sh` (Quest connected via
   USB-C, Developer Mode enabled).
2. On first launch, grant two permission prompts:
   - Camera
   - Horizon Camera (Meta's PCA — required for passthrough RGB frames)
3. With the sender app running, read the Quest's IP from the on-HUD
   `Host` line or from `Settings → Wi-Fi → (your network) → IP
   address`. Paste it into `QUEST_IP` at the top of `record.py`.

### Mac side

```bash
pip install "syncfield[uvc,viewer]"
```

`httpx` (used by `MetaQuestCameraStream`) and `numpy` are pulled in by
the viewer extra automatically.

### Network

- Quest and Mac on the **same WiFi subnet** (no AP isolation).
  Verify with `ping <quest_ip>` from the Mac.
- No firewall blocking UDP 14043 / 14044 or TCP 14045 between Mac and
  Quest.

---

## Run

```bash
python record.py
```

The SyncField viewer opens in your browser.

1. The `quest_tracking` stream card shows a **3-D hand skeleton panel**
   — cyan = left hand, orange = right hand, coloured axes = head pose.
   If it stays empty, the Quest is not sending (check the HUD, WiFi,
   and firewall).
2. The `quest_cam` card shows the live MJPEG preview (320×240 per
   eye) while the session is connected.
3. Click **Record** to start. All streams synchronise via the start
   chirp.
4. Click **Stop**. The Mac immediately pulls the recorded MP4s +
   per-eye timestamps JSONL off the Quest (a few seconds for short
   clips, up to tens of seconds for longer sessions).

---

## Output

```
output/<episode_id>/
├── sync_point.json
├── quest_tracking.jsonl               # 72 Hz hand + head samples
├── quest_cam_left.mp4                 # 720p MJPEG-AVI container
├── quest_cam_right.mp4
├── quest_cam_left.timestamps.jsonl    # one line per frame, host-monotonic ns
└── quest_cam_right.timestamps.jsonl
```

The MP4 files are MJPEG-in-AVI in phase 1 — VLC, ffmpeg, and
QuickTime open them out of the box. Phase 2 will swap in H.264
hardware encoding on the Quest side; the wire protocol and the Python
adapter both stay the same.

### Sample JSONL line (tracking)

```json
{
  "frame_number": 247,
  "capture_ns": 123456789012345,
  "clock_domain": "remote_quest3",
  "uncertainty_ns": 10000000,
  "channels": {
    "hand_joints":     [/* 156 floats */],
    "joint_rotations": [/* 208 floats */],
    "head_pose":       [px, py, pz, qx, qy, qz, qw]
  }
}
```

### Joint layout

OpenXR 26-joint order, per hand, 0-indexed:

```
 0: Palm           11: MiddleMetacarpal   21: LittleMetacarpal
 1: Wrist          12: MiddleProximal     22: LittleProximal
 2: ThumbMeta      13: MiddleIntermediate 23: LittleIntermediate
 3: ThumbProximal  14: MiddleDistal       24: LittleDistal
 4: ThumbDistal    15: MiddleTip          25: LittleTip
 5: ThumbTip       16: RingMetacarpal
 6: IndexMeta      17: RingProximal
 7: IndexProximal  18: RingIntermediate
 8: IndexInterm.   19: RingDistal
 9: IndexDistal    20: RingTip
10: IndexTip
```

Left hand fills `hand_joints[0:78]` (26 × 3), right `hand_joints[78:156]`.
Coordinate system: Unity left-handed Y-up (Quest's native OpenXR frame).

---

## Troubleshooting

### `quest_tracking` shows "Waiting for data…"
The Quest isn't sending UDP. Check:
- Is the sender app's HUD showing `● SENDING <hz>` (green)?
- On the Quest HUD, is the **Host** line the correct Mac IP?
- Mac firewall allowing inbound UDP 14043? (`System Settings → Network
  → Firewall`)

### `quest_cam` fails on stop with `httpx.ConnectError`
The stop-recording phase is when the Mac hits the Quest HTTP server.
- Is the Quest still on? (If you took the headset off mid-session,
  Quest might have gone to sleep and closed the socket.)
- Is `QUEST_IP` correct? Open `http://<QUEST_IP>:14045/status` in a
  browser — should return JSON. If it times out, the Quest's HTTP
  server is unreachable.
- Phase 1 records on-device and file-pulls at stop; if the session
  was very short or very long, try again or inspect `adb logcat -s
  Unity` during stop for the actual error.

### Video files exist but are 0 bytes
The Quest ran out of storage or `horizonos.permission.HEADSET_CAMERA`
was not granted. The sender app's HUD shows
`storage_free_bytes` on the `/status` endpoint; check and clear. Also
reinstall and re-grant permissions with
`scripts/build_and_deploy.sh` (it uninstalls + reinstalls fresh so
the permission prompts fire again).

### Hand-skeleton panel is mostly empty but tracking Hz looks fine
The Quest is sending UDP with `tracked: false` flags because your
hands are out of the headset's tracking volume. Move them back into
view — the panel should populate within a frame or two.
