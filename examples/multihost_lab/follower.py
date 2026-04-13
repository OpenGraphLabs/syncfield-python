"""Multi-host lab follower.

Run on every machine other than the leader. Blocks until the leader's
session is recording, then mirrors its lifecycle. No UI — the operator
sits at the leader's MacBook; followers are headless.

    pip install "syncfield[multihost,uvc,audio]"
    python follower.py

The SDK auto-injects a host audio stream so the leader's chirp gets
captured locally for post-hoc sync. Declare only your own streams.
"""

from pathlib import Path

import syncfield as sf
from syncfield.adapters import UVCWebcamStream


session = sf.SessionOrchestrator(
    host_id="mac_b",  # change per host: mac_c, mac_d, …
    output_dir=Path(__file__).parent / "output",
    role=sf.FollowerRole(),  # no session_id → auto-discover the leader on LAN
)

session.add(UVCWebcamStream("mac_webcam", device_index=0, output_dir=session.output_dir))
session.add(UVCWebcamStream("iphone",     device_index=1, output_dir=session.output_dir))


if __name__ == "__main__":
    print("Follower waiting for leader…")
    session.start()  # blocks until the leader is recording
    print(f"Attached to leader {session.observed_leader.host_id}")
    print(f"Session id: {session.session_id}")

    session.wait_for_leader_stopped()  # blocks until leader stops
    session.stop()
    print("Follower done. Files are local; leader will pull them shortly.")
