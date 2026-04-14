"""Unit tests for QuestHttpClient — no real Quest required."""

from __future__ import annotations

import json

import httpx
import pytest

from syncfield.adapters.meta_quest_camera.http_client import (
    QuestHttpClient,
    QuestStatus,
    RecordingStartResponse,
    RecordingStopResponse,
    RecordingAlreadyActive,
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


class TestStartRecording:
    def test_start_recording_happy_path(self, quest_host, quest_port):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/recording/start"
            body = json.loads(request.content)
            assert body["session_id"] == "ep_x"
            assert body["host_mono_ns"] == 111
            assert body["resolution"] == {"width": 1280, "height": 720}
            assert body["fps"] == 30
            return httpx.Response(
                200,
                json={
                    "session_id": "ep_x",
                    "quest_mono_ns_at_start": 42,
                    "delta_ns": 69,
                    "started": True,
                },
            )

        client = QuestHttpClient(
            host=quest_host, port=quest_port, transport=_mock_transport(handler)
        )
        res = client.start_recording(
            session_id="ep_x", host_mono_ns=111, width=1280, height=720, fps=30
        )
        assert isinstance(res, RecordingStartResponse)
        assert res.session_id == "ep_x"
        assert res.delta_ns == 69
        assert res.started is True

    def test_start_recording_409_raises(self, quest_host, quest_port):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(409, json={"error": "session_already_active"})

        client = QuestHttpClient(
            host=quest_host, port=quest_port, transport=_mock_transport(handler)
        )
        with pytest.raises(RecordingAlreadyActive):
            client.start_recording(
                session_id="ep_x", host_mono_ns=1, width=1280, height=720, fps=30
            )


class TestStopRecording:
    def test_stop_recording_happy_path(self, quest_host, quest_port):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/recording/stop"
            return httpx.Response(
                200,
                json={
                    "session_id": "ep_x",
                    "left":  {"frame_count": 100, "bytes": 1000, "last_capture_ns": 9},
                    "right": {"frame_count": 100, "bytes": 1001, "last_capture_ns": 9},
                    "duration_s": 3.33,
                },
            )

        client = QuestHttpClient(
            host=quest_host, port=quest_port, transport=_mock_transport(handler)
        )
        res = client.stop_recording()
        assert isinstance(res, RecordingStopResponse)
        assert res.left.frame_count == 100
        assert res.right.frame_count == 100
        assert res.duration_s == pytest.approx(3.33)
