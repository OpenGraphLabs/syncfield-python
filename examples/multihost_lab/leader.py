"""Multi-host lab leader.

Run this on the machine with the primary camera(s) + speaker. Every
other machine on the same LAN runs `follower.py`.

    pip install "syncfield[multihost,uvc,audio]"
    python leader.py

The SDK auto-injects a host audio stream (your built-in mic) on every
multi-host host so the leader's chirp gets captured for post-hoc
cross-correlation alignment. You only need to declare your own
data-bearing streams (cameras, sensors) — audio is automatic.
"""

from pathlib import Path

import syncfield as sf
from syncfield.adapters import UVCWebcamStream


session = sf.SessionOrchestrator(
    host_id="mac_a",
    output_dir=Path(__file__).parent / "output",
    role=sf.LeaderRole(session_id="lab_session"),
)

session.add(UVCWebcamStream("mac_webcam", device_index=0, output_dir=session.output_dir))
session.add(UVCWebcamStream("iphone",     device_index=1, output_dir=session.output_dir))


if __name__ == "__main__":
    import time

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
        print(f"Collected from {len(report['hosts'])} host(s):")
        for host in report["hosts"]:
            print(f"  - {host['host_id']}: {host['status']} "
                  f"({len(host.get('files', []))} files)")
