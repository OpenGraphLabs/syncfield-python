"""PollingSensorStream demo — a fake serial sensor at 50 Hz."""

from __future__ import annotations

import math
import time
from pathlib import Path

import syncfield as sf
from syncfield.adapters import PollingSensorStream


class FakeSerial:
    """Stand-in for a real serial.Serial — emits a sine wave."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def read_sample(self) -> dict[str, float]:
        t = time.monotonic() - self._t0
        return {
            "ax": math.sin(t * 2 * math.pi),
            "ay": math.cos(t * 2 * math.pi),
            "az": 0.5 * math.sin(t * 4 * math.pi),
        }

    def close(self) -> None:
        pass


def main() -> None:
    output_dir = Path("./demo_session_polling")
    output_dir.mkdir(exist_ok=True)

    session = sf.SessionOrchestrator(host_id="demo", output_dir=output_dir)

    serial = FakeSerial()
    session.add(PollingSensorStream("fake_imu", read=serial.read_sample, hz=50))

    session.connect()
    session.start(countdown_s=0)
    print("Recording for 2 seconds...")
    time.sleep(2.0)
    report = session.stop()
    session.disconnect()

    serial.close()

    for f in report.finalizations:
        print(f"  {f.stream_id}: {f.frame_count} samples")


if __name__ == "__main__":
    main()
