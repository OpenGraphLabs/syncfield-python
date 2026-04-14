"""Integration test: full record → enqueue → aggregate → on-disk artifacts."""
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


class FakeBleCamera:
    def __init__(self, address: str):
        self.address = address
        self.is_connected = False
        self.ble_name = "GO 3S TEST"

    async def connect(self, sync_timeout: float = 2.0, auth_timeout: float = 1.0, discovery_timeout: float = 5.0):
        self.is_connected = True

    async def set_video_mode(self):
        pass

    async def start_capture(self) -> int:
        return 12345

    async def stop_capture(self) -> CaptureResult:
        return CaptureResult(file_path="/DCIM/Camera01/VID_E2E.mp4", ack_host_ns=23456)

    async def disconnect(self):
        self.is_connected = False


class FakeDownloader(AggregationDownloader):
    async def run(self, camera, target_dir, on_chunk, on_stage=None):
        target = target_dir / camera.local_filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"VID_E2E_FAKE_CONTENT")
        on_chunk(camera.stream_id, len(b"VID_E2E_FAKE_CONTENT"), len(b"VID_E2E_FAKE_CONTENT"))


@pytest.mark.asyncio
async def test_e2e_record_then_aggregate(tmp_path):
    queue = AggregationQueue(downloader=FakeDownloader())
    await queue.start()
    try:
        with (
            patch("syncfield.adapters.insta360_go3s.stream.Go3SBLECamera", FakeBleCamera),
            patch(
                "syncfield.adapters.insta360_go3s.stream._global_aggregation_queue",
                lambda: queue,
            ),
        ):
            ep_dir = tmp_path / "ep_e2e"
            ep_dir.mkdir()
            session = SessionOrchestrator(host_id="mac", output_dir=tmp_path)
            stream = Go3SStream(
                stream_id="overhead",
                ble_address="AA:BB:CC:DD:EE:FF",
                output_dir=ep_dir,
            )
            session.add(stream)
            # Stream lifecycle methods call asyncio.run() internally, which
            # cannot be called from a running event loop. Run them in a thread.
            await asyncio.to_thread(stream.prepare)
            await asyncio.to_thread(stream.connect)
            await asyncio.to_thread(stream.start_recording, None)  # type: ignore[arg-type]
            report = await asyncio.to_thread(stream.stop_recording)
            await asyncio.to_thread(stream.disconnect)

            assert report.status == "pending_aggregation"
            assert stream.pending_aggregation_job is not None

            # Wait for the queue worker to flush the job
            for _ in range(50):
                if (ep_dir / "overhead.mp4").exists():
                    break
                await asyncio.sleep(0.1)
            assert (ep_dir / "overhead.mp4").exists()
            manifest = json.loads((ep_dir / "aggregation.json").read_text())
            assert manifest["state"] == "completed"
            assert manifest["cameras"][0]["done"] is True
    finally:
        await queue.shutdown()
