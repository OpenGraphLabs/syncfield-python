"""Local test follower — matches leader.py but auto-discovers."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import syncfield as sf
from syncfield.testing import FakeStream


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-id", default="mac_b")
    parser.add_argument("--session-id", default="local-test-cluster",
                        help="Pre-shared session_id. Pass empty string '' for auto-discover.")
    parser.add_argument("--output-dir", default="./data_follower")
    parser.add_argument("--control-plane-port", type=int, default=0,
                        help="0 means OS-assigned (avoids collisions on localhost).")
    parser.add_argument("--leader-wait-timeout-sec", type=float, default=60.0)
    parser.add_argument("--keep-alive-sec", type=float, default=30.0)
    parser.add_argument(
        "--leader",
        metavar="HOST_ID@ADDRESS:PORT",
        help="Static leader address (bypasses mDNS — required on macOS "
             "single-machine testing). Format: 'mac_a@127.0.0.1:7878'.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    role = sf.FollowerRole(
        session_id=args.session_id or None,
        leader_wait_timeout_sec=args.leader_wait_timeout_sec,
        control_plane_port=args.control_plane_port,
        keep_alive_after_stop_sec=args.keep_alive_sec,
    )

    session = sf.SessionOrchestrator(
        host_id=args.host_id,
        output_dir=Path(args.output_dir),
        role=role,
        sync_tone=sf.SyncToneConfig.silent(),
    )

    session.add(FakeStream("wrist_cam"))
    mic = FakeStream("mic")
    mic.kind = "audio"
    session.add(mic)

    if args.leader:
        if "@" not in args.leader or ":" not in args.leader:
            parser.error("--leader must be HOST_ID@ADDRESS:PORT, got: " + args.leader)
        host_part, address_part = args.leader.split("@", 1)
        address, port_str = address_part.rsplit(":", 1)
        session.set_static_leader(
            host_id=host_part,
            address=address,
            control_plane_port=int(port_str),
        )
        print(f"[follower {args.host_id}] static leader configured: "
              f"{host_part}@{address}:{port_str} (mDNS bypass)")

    print(f"[follower {args.host_id}] waiting for leader…")
    session.start()
    port = session._control_plane.actual_port
    print(f"[follower {args.host_id}] attached to leader {session.observed_leader.host_id}, "
          f"session_id={session.session_id}, control plane :{port}")

    print(f"[follower {args.host_id}] waiting for leader to stop…")
    session.wait_for_leader_stopped()
    session.stop()
    print(f"[follower {args.host_id}] stopped. Control plane still up for {args.keep_alive_sec}s "
          f"so leader can pull files…")

    # Keep alive timer will tear this down automatically. Or the
    # leader's DELETE /session preempts it. Either way, block here
    # until the control plane self-terminates.
    import time
    deadline = time.monotonic() + args.keep_alive_sec + 5.0
    while time.monotonic() < deadline and session._control_plane is not None and session._control_plane.is_running:
        time.sleep(0.2)
    print(f"[follower {args.host_id}] done.")


if __name__ == "__main__":
    main()
