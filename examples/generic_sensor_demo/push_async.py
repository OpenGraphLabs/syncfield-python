"""PushSensorStream demo — an asyncio task pushes samples at 100 Hz."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from pathlib import Path

import syncfield as sf
from syncfield.adapters import PushSensorStream


def main() -> None:
    output_dir = Path("./demo_session_push")
    output_dir.mkdir(exist_ok=True)

    session = sf.SessionOrchestrator(host_id="demo", output_dir=output_dir)

    loop_holder: dict = {}
    stop_event = asyncio.Event()

    async def fake_ble_loop(stream: PushSensorStream) -> None:
        t0 = time.monotonic()
        while not stop_event.is_set():
            t = time.monotonic() - t0
            stream.push({
                "ax": math.sin(t * 2 * math.pi),
                "ay": math.cos(t * 2 * math.pi),
            })
            await asyncio.sleep(0.01)

    def on_connect(stream: PushSensorStream) -> None:
        def run():
            loop = asyncio.new_event_loop()
            loop_holder["loop"] = loop
            asyncio.set_event_loop(loop)
            loop.run_until_complete(fake_ble_loop(stream))
            loop.close()
        thread = threading.Thread(target=run, daemon=True)
        loop_holder["thread"] = thread
        thread.start()

    def on_disconnect(stream: PushSensorStream) -> None:
        loop = loop_holder.get("loop")
        if loop is not None:
            loop.call_soon_threadsafe(stop_event.set)
        thread = loop_holder.get("thread")
        if thread is not None:
            thread.join(timeout=1.0)

    push_stream = PushSensorStream(
        "fake_ble_imu",
        on_connect=on_connect,
        on_disconnect=on_disconnect,
    )
    session.add(push_stream)

    session.connect()
    session.start(countdown_s=0)
    print("Recording for 2 seconds...")
    time.sleep(2.0)
    report = session.stop()
    session.disconnect()

    for f in report.finalizations:
        print(f"  {f.stream_id}: {f.frame_count} samples")


if __name__ == "__main__":
    main()
