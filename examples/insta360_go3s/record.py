"""Single-host recording with one Insta360 Go3S camera.

Usage:
    uv run python examples/insta360_go3s/record.py \\
        --address AA:BB:CC:DD:EE:FF \\
        --output ./go3s_output \\
        --duration 10
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import syncfield as sf
from syncfield.adapters.insta360_go3s import Go3SStream


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True, help="Go3S BLE address (MAC or CB UUID)")
    parser.add_argument("--output", type=Path, default=Path("./go3s_output"))
    parser.add_argument("--duration", type=float, default=10.0)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)

    session = sf.SessionOrchestrator(host_id="local", output_dir=args.output)
    session.add(Go3SStream(
        stream_id="overhead",
        ble_address=args.address,
        output_dir=args.output,
    ))

    print(f"[record] starting session, duration={args.duration}s")
    session.start_recording()
    time.sleep(args.duration)
    report = session.stop_recording()
    print(f"[record] stopped; per-stream reports: {report}")
    print("[record] aggregation runs in the background; check the viewer or look in",
          args.output)


if __name__ == "__main__":
    main()
