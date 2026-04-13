"""Local test leader — uses FakeStream for audio so it runs on any dev machine.

Run this in one terminal, then run follower.py in another (twice, for a
3-host test). All three processes share localhost; mDNS works on
loopback, so discovery Just Works.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import syncfield as sf
from syncfield.testing import FakeStream


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-id", default="mac_a")
    parser.add_argument("--session-id", default="local-test-cluster")
    parser.add_argument("--output-dir", default="./data_leader")
    parser.add_argument("--control-plane-port", type=int, default=7878)
    parser.add_argument("--recording-seconds", type=float, default=5.0,
                        help="How long to record before stopping. 0 = wait for Ctrl-C.")
    parser.add_argument("--keep-alive-sec", type=float, default=30.0,
                        help="How long the control plane stays up post-stop so followers can be pulled from.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    session = sf.SessionOrchestrator(
        host_id=args.host_id,
        output_dir=Path(args.output_dir),
        role=sf.LeaderRole(
            session_id=args.session_id,
            control_plane_port=args.control_plane_port,
            keep_alive_after_stop_sec=args.keep_alive_sec,
        ),
        sync_tone=sf.SyncToneConfig.silent(),  # no real chirp on local tests
    )

    # FakeStreams — one video, one audio. Audio is required by the
    # multi-host audio gate. No real hardware or files involved.
    session.add(FakeStream("cam_main"))
    mic = FakeStream("mic_builtin")
    mic.kind = "audio"
    session.add(mic)

    print(f"[leader {args.host_id}] starting session {session.session_id}")
    session.start()
    port = session._control_plane.actual_port
    print(f"[leader {args.host_id}] control plane listening on :{port}")

    if args.recording_seconds > 0:
        print(f"[leader {args.host_id}] recording for {args.recording_seconds:.1f}s…")
        time.sleep(args.recording_seconds)
    else:
        print(f"[leader {args.host_id}] recording until Ctrl-C…")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    print(f"[leader {args.host_id}] stopping…")
    session.stop()

    print(f"[leader {args.host_id}] collecting files from followers (inside {args.keep_alive_sec}s window)…")
    report = session.collect_from_followers()
    print(f"[leader {args.host_id}] aggregated report:")
    print(f"  session_id: {report['session_id']}")
    print(f"  leader_host_id: {report['leader_host_id']}")
    for host in report["hosts"]:
        print(f"  {host['host_id']}: status={host['status']} files={len(host['files'])}")
        if host.get("error"):
            print(f"    error: {host['error']}")

    # Control plane keeps running for keep_alive_sec — force stop so
    # the process exits cleanly rather than lingering.
    session._force_stop_control_plane()
    print(f"[leader {args.host_id}] done.")


if __name__ == "__main__":
    main()
