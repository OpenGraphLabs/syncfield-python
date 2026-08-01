"""End-to-end: adapter + SessionOrchestrator + FakeQuestServer."""

from __future__ import annotations

import asyncio
import io
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest
from PIL import Image

from syncfield import SessionOrchestrator, SyncToneConfig
from syncfield.adapters import MetaQuestCameraStream
from syncfield.adapters.meta_quest_camera.preview import MjpegFrame
from tests.helpers.fake_quest_server import FakeQuestServer


def _make_jpeg() -> bytes:
    image = Image.new("RGB", (64, 64), color=(120, 50, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def _run_session(
    orch: SessionOrchestrator,
    stream: MetaQuestCameraStream,
) -> None:
    """Run the full orchestrator lifecycle synchronously (called in a thread)."""
    orch.connect()
    orch.start(countdown_s=0)
    jpeg = _make_jpeg()
    base_ns = time.monotonic_ns()
    for index in range(5):
        host_ns = base_ns + index * 33_333_333
        stream._preview._frame_sink(
            MjpegFrame(
                jpeg_bytes=jpeg,
                capture_ns=host_ns,
                quest_native_ns=host_ns - 1_000_000,
            )
        )
    orch.stop()
    orch.disconnect()


@pytest.mark.asyncio
async def test_orchestrator_drives_adapter_end_to_end(tmp_path: Path):
    server = FakeQuestServer(left_mp4=b"LEFT_PAYLOAD", right_mp4=b"RIGHT_PAYLOAD")
    async with server.run() as base_url:
        parsed = urlparse(base_url)
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host=parsed.hostname,
            quest_port=parsed.port,
            output_dir=tmp_path,
        )
        orch = SessionOrchestrator(
            host_id="test-host",
            output_dir=tmp_path,
            sync_tone=SyncToneConfig.silent(),
        )
        orch.add(stream)
        # Run the blocking synchronous orchestrator lifecycle in a thread so
        # the asyncio event loop (which drives the aiohttp FakeQuestServer)
        # remains free to handle the adapter's HTTP requests.
        await asyncio.to_thread(_run_session, orch, stream)

    # The current Quest adapter records one streamed eye into the episode.
    # Completed recordings live at last_episode_dir; output_dir has already
    # rotated to the next episode by the time stop() returns.
    out = orch.last_episode_dir
    assert out is not None
    assert (out / "quest_cam.mp4").stat().st_size > 0
    assert (out / "quest_cam.timestamps.jsonl").exists()
    assert not (out / "quest_cam_left.mp4").exists()
    assert not (out / "quest_cam_right.mp4").exists()
