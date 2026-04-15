"""End-to-end: adapter + SessionOrchestrator + FakeQuestServer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

import pytest

from syncfield import SessionOrchestrator, SyncToneConfig
from syncfield.adapters import MetaQuestCameraStream
from tests.helpers.fake_quest_server import FakeQuestServer


def _run_session(orch: SessionOrchestrator) -> None:
    """Run the full orchestrator lifecycle synchronously (called in a thread)."""
    orch.connect()
    orch.start(countdown_s=0)
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
        await asyncio.to_thread(_run_session, orch)

    # Confirm artifacts landed in the stream's own output_dir (tmp_path).
    assert (tmp_path / "quest_cam_left.mp4").read_bytes() == b"LEFT_PAYLOAD"
    assert (tmp_path / "quest_cam_right.mp4").read_bytes() == b"RIGHT_PAYLOAD"
    assert (tmp_path / "quest_cam_left.timestamps.jsonl").exists()
    assert (tmp_path / "quest_cam_right.timestamps.jsonl").exists()
