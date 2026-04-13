"""Multi-host lab leader.

Run this on the machine with the primary camera + speaker. Every
other machine on the same LAN runs `follower.py`.
"""

from __future__ import annotations

import time
from pathlib import Path

import syncfield as sf
from syncfield.adapters.host_audio import HostAudioStream
from syncfield.adapters.uvc_webcam import UVCWebcamStream


def main() -> None:
    session = sf.SessionOrchestrator(
        host_id="mac_a",
        output_dir=Path("./data"),
        role=sf.LeaderRole(session_id="lab_session"),
    )

    # Every multi-host host needs at least one audio-capable stream —
    # the leader records its own chirp, followers record it arriving
    # through the air.
    session.add(UVCWebcamStream("cam_main", device_index=0, output_dir=Path("./data")))
    session.add(HostAudioStream("mic_builtin", output_dir=Path("./data")))

    print(f"Leader starting session {session.session_id}…")
    session.start()
    print("Recording. Press Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping…")
        session.stop()
        print("Collecting files from followers…")
        report = session.collect_from_followers()
        print(f"Collected from {len(report['hosts'])} follower(s):")
        for host in report["hosts"]:
            print(f"  - {host['host_id']}: {host['status']}")


if __name__ == "__main__":
    main()
