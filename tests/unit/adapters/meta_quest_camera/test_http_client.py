"""Unit tests for QuestHttpClient — no real Quest required."""

from __future__ import annotations

import json

import httpx
import pytest

from syncfield.adapters.meta_quest_camera.http_client import (
    QuestHttpClient,
    QuestStatus,
)


def _mock_transport(handler):
    return httpx.MockTransport(handler)


class TestStatus:
    def test_status_returns_parsed_snapshot(self, quest_host, quest_port):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path == "/status"
            return httpx.Response(
                200,
                json={
                    "recording": False,
                    "session_id": None,
                    "last_preview_capture_ns": 123,
                    "left_camera_ready": True,
                    "right_camera_ready": True,
                    "storage_free_bytes": 42_000_000_000,
                },
            )

        client = QuestHttpClient(
            host=quest_host, port=quest_port, transport=_mock_transport(handler)
        )
        snap = client.status()
        assert isinstance(snap, QuestStatus)
        assert snap.recording is False
        assert snap.left_camera_ready is True
        assert snap.right_camera_ready is True
        assert snap.storage_free_bytes == 42_000_000_000
