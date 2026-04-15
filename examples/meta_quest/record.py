"""Record Meta Quest 3 head pose + hand tracking + stereo egocentric camera.

    pip install "syncfield[uvc,viewer]"
    python record.py

Workflow:
  1. Put on the Quest 3 headset.
  2. Open the SyncField Quest Sender app on the Quest (sideloaded from
     ``opengraph-studio/unity/SyncFieldQuest3Sender``). Confirm the HUD
     shows ``● SENDING <hz>`` in green.
  3. Confirm the Quest and this Mac are on the same WiFi subnet, then
     copy the Quest's IP address from the HUD into ``QUEST_IP`` below.
  4. Click **Record** in the viewer → every stream starts in sync.
  5. Click **Stop** → the Mac pulls the recorded stereo MP4 + per-eye
     timestamps JSONL off the Quest over HTTP (takes a few seconds).

What the session captures:

  ``quest_tracking``             (MetaQuestHandStream, UDP 14043)
      ``quest_tracking.jsonl``   — 72 Hz samples of 26 OpenXR hand
                                   joints × 3 xyz × 2 hands (156 floats)
                                   + per-joint quaternions (208 floats)
                                   + head pose (pos3 + quat4)

  ``quest_cam``                  (MetaQuestCameraStream, HTTP 14045)
      ``quest_cam_left.mp4``
      ``quest_cam_right.mp4``    — 720p @ 30 fps passthrough video
      ``quest_cam_*.timestamps.jsonl`` — per-frame host-monotonic ns

Everything lands under ``output/<episode_id>/``. Timestamps are
projected into the host monotonic clock domain by the Quest app so
offline sync tooling can align against the Mac's other sensors
without any extra calibration step.

HUD signals (Quest-side):
  ● SENDING  72 Hz · <N> pkts   tracking flowing, receiver reachable
  ● STALLED  <seconds> no ack   last UDP ack >0.5 s ago
  ● NO HOST                     no tracking ack for >2 s; check WiFi

Pinch both hands for 2 s on the Quest to exit the sender app from
inside a hand-tracking session (the Meta system menu can be awkward
to reach with recording gloves / straps on).
"""

from pathlib import Path

import syncfield as sf
import syncfield.viewer
from syncfield.adapters import MetaQuestCameraStream, MetaQuestHandStream

# Quest 3 IPv4 — copy from the Quest sender app's HUD ("Host" line) or
# check Settings → Wi-Fi → <network> → Details → IP address. Must be on
# the same subnet as this Mac. Camera file pull uses HTTP :14045; there
# is no auto-discovery yet (tracking UDP does auto-discover, but the
# camera HTTP endpoint needs an explicit host).
QUEST_IP = "192.168.4.26"

session = sf.SessionOrchestrator(
    host_id="mac_studio",
    output_dir=Path(__file__).parent / "output",
)

# Head + hand tracking over UDP. Defaults to listening on
# 0.0.0.0:14043 — no IP needed here because the Quest app sends TO
# this Mac after the Mac broadcasts a discovery probe on :14044.
session.add(MetaQuestHandStream(
    "quest_tracking",
    mode="hand",  # or "controller" to map Touch Plus pose into wrist slots
    quest_host=QUEST_IP,  # push our IP via Quest HTTP — no broadcast needed
))

# Stereo passthrough camera. We pull MP4 + timestamps over HTTP on
# stop_recording() — this path DOES need an explicit Quest IP because
# the Mac initiates the request.
session.add(MetaQuestCameraStream(
    "quest_cam",
    quest_host=QUEST_IP,
    output_dir=session.output_dir,
    fps=30,
    resolution=(1280, 720),
))

syncfield.viewer.launch(session)
