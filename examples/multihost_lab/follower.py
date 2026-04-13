"""Multi-host lab follower.

Run on every machine other than the leader. Blocks until the leader's
session is recording, then mirrors its lifecycle.
"""

from __future__ import annotations

from pathlib import Path

import syncfield as sf
from syncfield.adapters.host_audio import HostAudioStream
from syncfield.adapters.uvc_webcam import UVCWebcamStream


def main() -> None:
    session = sf.SessionOrchestrator(
        host_id="mac_b",  # change per host — mac_c, mac_d, …
        output_dir=Path("./data"),
        role=sf.FollowerRole(),  # no session_id — auto-discover the leader
    )

    # Audio stream required on every host for chirp capture.
    session.add(UVCWebcamStream("wrist_cam", device_index=0, output_dir=Path("./data")))
    session.add(HostAudioStream("mic", output_dir=Path("./data")))

    print("Follower waiting for leader…")
    session.start()  # blocks until the leader is recording
    print(f"Attached to leader {session.observed_leader.host_id}")
    print(f"Session id: {session.session_id}")

    session.wait_for_leader_stopped()  # blocks until leader stops
    session.stop()
    print("Follower done. Files are local; leader will pull them shortly.")


if __name__ == "__main__":
    main()
