"""Multi-host lab leader.

Run this on the machine with the primary camera(s) + speaker. Every
other machine on the same LAN runs `follower.py`.

    pip install "syncfield[multihost,uvc,audio,viewer]"
    python leader.py

The SyncField web viewer opens in your browser. It shows:
  - local stream previews (mac_webcam, iphone, auto-injected mic)
  - a Cluster panel (right sidebar) listing every follower it discovers
    on mDNS, their live fps/dropped/disk health, and RTT to each host
  - Record / Stop buttons — clicking Record starts the whole cluster
    atomically via mDNS: leader plays chirp, followers auto-attach
  - a leader-only 'Collect Data' button — after Stop, this pulls every
    follower's recorded files into a flat
    ./output/<session_id>/<leader_ep>/<host>.<filename> tree

Audio is auto-injected by the SDK on every multi-host host so the
leader's chirp gets captured for post-hoc cross-correlation alignment.
Declare only your own data-bearing streams below.
"""

from pathlib import Path

import syncfield as sf
import syncfield.viewer
from syncfield.adapters import UVCWebcamStream


session = sf.SessionOrchestrator(
    host_id="mac_a",
    output_dir=Path(__file__).parent / "output",
    role=sf.LeaderRole(session_id="lab_session"),
)

session.add(UVCWebcamStream("mac_webcam", device_index=0, output_dir=session.output_dir))
session.add(UVCWebcamStream("iphone",     device_index=1, output_dir=session.output_dir))

# mDNS is already broadcasting — the advertiser + control plane came
# up at SessionOrchestrator construction time, so the leader is
# discoverable the moment this script starts. The viewer's Connect
# button opens devices for live preview; mDNS doesn't depend on that.
syncfield.viewer.launch(session)
