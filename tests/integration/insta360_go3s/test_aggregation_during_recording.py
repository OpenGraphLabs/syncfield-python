"""Integration test: aggregation in-flight does not block subsequent recordings."""
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from syncfield.adapters.insta360_go3s import Go3SStream
from syncfield.adapters.insta360_go3s.aggregation.queue import (
    AggregationDownloader,
    AggregationQueue,
)
from syncfield.adapters.insta360_go3s.ble.camera import CaptureResult
from syncfield.orchestrator import SessionOrchestrator


class SlowDownloader(AggregationDownloader):
    """Holds the WiFi for ~0.5s so a second recording fires while it's busy."""

    def __init__(self):
        self.in_flight = asyncio.Event()
        self.may_finish = asyncio.Event()
        self.completed: list[str] = []

    async def run(self, camera, target_dir, on_chunk, on_stage=None):
        self.in_flight.set()
        await self.may_finish.wait()
        target = target_dir / camera.local_filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"X" * 32)
        on_chunk(camera.stream_id, 32, 32)
        self.completed.append(camera.stream_id)


class FakeBleCamera:
    def __init__(self, address):
        self.address = address
        self.is_connected = False
        self.ble_name = "GO 3S TEST"
    async def connect(self, sync_timeout=2.0, auth_timeout=1.0, discovery_timeout=5.0):
        self.is_connected = True
    async def set_video_mode(self): pass
    async def start_capture(self): return 1
    async def stop_capture(self): return CaptureResult(file_path="/DCIM/Camera01/VID.mp4", ack_host_ns=2)
    async def disconnect(self): self.is_connected = False


@pytest.mark.asyncio
async def test_recording_succeeds_while_aggregation_runs(tmp_path):
    downloader = SlowDownloader()
    queue = AggregationQueue(downloader=downloader)
    await queue.start()
    try:
        with (
            patch("syncfield.adapters.insta360_go3s.stream.Go3SBLECamera", FakeBleCamera),
            patch(
                "syncfield.adapters.insta360_go3s.stream._global_aggregation_queue",
                lambda: queue,
            ),
        ):
            session = SessionOrchestrator(host_id="mac", output_dir=tmp_path)
            ep1 = tmp_path / "ep1"; ep1.mkdir()
            ep2 = tmp_path / "ep2"; ep2.mkdir()
            stream = Go3SStream(
                stream_id="overhead",
                ble_address="AA:BB:CC:DD:EE:FF",
                output_dir=ep1,
                aggregation_policy="eager",
            )
            session.add(stream)

            # Episode 1 — wrap sync calls in to_thread (Go3SStream uses asyncio.run internally)
            await asyncio.to_thread(stream.prepare)
            await asyncio.to_thread(stream.connect)
            await asyncio.to_thread(stream.start_recording, None)  # type: ignore[arg-type]
            await asyncio.to_thread(stream.stop_recording)  # enqueues episode 1

            # Wait for downloader to be mid-flight
            await asyncio.wait_for(downloader.in_flight.wait(), timeout=2.0)

            # Episode 2 — start while episode 1's download is in-flight
            stream._output_dir = ep2  # simulate orchestrator advancing episode dir
            # Reset the in_flight flag so we can detect the second job's start later
            downloader.in_flight.clear()
            await asyncio.to_thread(stream.start_recording, None)  # type: ignore[arg-type]
            report2 = await asyncio.to_thread(stream.stop_recording)
            assert report2.status == "pending_aggregation"

            # Now release the slow downloader so both episodes can finish
            downloader.may_finish.set()

            for _ in range(80):
                if (ep1 / "overhead.mp4").exists() and (ep2 / "overhead.mp4").exists():
                    break
                await asyncio.sleep(0.05)
            assert (ep1 / "overhead.mp4").exists(), "episode 1 should have downloaded"
            assert (ep2 / "overhead.mp4").exists(), "episode 2 should have downloaded"
            assert "overhead" in downloader.completed, "downloader should have run"
    finally:
        await queue.shutdown()
