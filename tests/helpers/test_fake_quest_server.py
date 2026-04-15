"""Sanity test for the in-process fake Quest HTTP server."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.fake_quest_server import FakeQuestServer


@pytest.mark.asyncio
async def test_fake_server_serves_status_and_recording_roundtrip(tmp_path):
    server = FakeQuestServer(left_mp4=b"LEFT", right_mp4=b"RIGHT")
    async with server.run() as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            r = await client.get("/status")
            assert r.status_code == 200
            assert r.json()["left_camera_ready"] is True

            r = await client.post("/recording/start", json={
                "session_id": "ep_t", "host_mono_ns": 1,
                "resolution": {"width": 1280, "height": 720}, "fps": 30,
            })
            assert r.status_code == 200

            r = await client.post("/recording/stop", json={})
            assert r.status_code == 200

            r = await client.get("/recording/files/left")
            assert r.status_code == 200
            assert r.content == b"LEFT"
