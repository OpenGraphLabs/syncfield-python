"""Integration test: atomic failure preserves originals; retry succeeds."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from syncfield.adapters.insta360_go3s import Go3SStream
from syncfield.adapters.insta360_go3s.aggregation.queue import (
    AggregationDownloader,
    AggregationQueue,
)
from syncfield.adapters.insta360_go3s.aggregation.types import AggregationState
from syncfield.adapters.insta360_go3s.ble.camera import CaptureResult
from syncfield.orchestrator import SessionOrchestrator


class FlakyDownloader(AggregationDownloader):
    """Fails the first time, succeeds on retry."""

    def __init__(self):
        self.attempts = 0

    async def run(self, camera, target_dir, on_chunk):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("simulated WiFi switch failure")
        target = target_dir / camera.local_filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"OK")
        on_chunk(camera.stream_id, 2, 2)


class FakeBleCamera:
    def __init__(self, address):
        self.address = address
        self.is_connected = False
    async def connect(self, sync_timeout=2.0, auth_timeout=1.0):
        self.is_connected = True
    async def set_video_mode(self): pass
    async def start_capture(self): return 1
    async def stop_capture(self): return CaptureResult(file_path="/DCIM/Camera01/VID.mp4", ack_host_ns=2)
    async def disconnect(self): self.is_connected = False


@pytest.mark.asyncio
async def test_failure_then_retry(tmp_path):
    downloader = FlakyDownloader()
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
            ep = tmp_path / "ep_fail"; ep.mkdir()
            stream = Go3SStream(
                stream_id="overhead",
                ble_address="AA:BB:CC:DD:EE:FF",
                output_dir=ep,
            )
            session.add(stream)
            await asyncio.to_thread(stream.prepare)
            await asyncio.to_thread(stream.connect)
            await asyncio.to_thread(stream.start_recording, None)  # type: ignore[arg-type]
            await asyncio.to_thread(stream.stop_recording)
            assert stream.pending_aggregation_job is not None
            job_id = stream.pending_aggregation_job.job_id

            # Wait for first failure to be persisted to aggregation.json
            for _ in range(40):
                if downloader.attempts >= 1:
                    break
                await asyncio.sleep(0.05)
            for _ in range(40):
                manifest_path = ep / "aggregation.json"
                if manifest_path.exists():
                    manifest = json.loads(manifest_path.read_text())
                    if manifest["state"] == "failed":
                        break
                await asyncio.sleep(0.05)
            assert manifest["state"] == "failed"
            assert not (ep / "overhead.mp4").exists(), \
                "no partial file should be left after failure"

            # Retry — invokes queue.retry directly (the orchestrator-level
            # retry_aggregation also delegates here, but the queue's retry
            # is what actually runs the worker again)
            handle = queue.retry(job_id)
            final = await handle.wait()
            assert final.state == AggregationState.COMPLETED
            assert (ep / "overhead.mp4").exists()
            # Confirm manifest is updated
            manifest = json.loads((ep / "aggregation.json").read_text())
            assert manifest["state"] == "completed"
    finally:
        await queue.shutdown()
