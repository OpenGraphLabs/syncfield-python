"""Unit tests for the top-level MetaQuestCameraStream adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncfield.adapters.meta_quest_camera import MetaQuestCameraStream


class TestIdentity:
    def test_stream_identity_and_capabilities(self, tmp_path: Path):
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="192.0.2.10",
            output_dir=tmp_path,
        )
        assert stream.id == "quest_cam"
        assert stream.kind == "video"
        assert stream.capabilities.produces_file is True
        assert stream.capabilities.supports_precise_timestamps is True
        assert stream.capabilities.is_removable is True
        assert stream.capabilities.provides_audio_track is False

    def test_device_key_includes_host(self, tmp_path: Path):
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="192.0.2.10",
            output_dir=tmp_path,
        )
        assert stream.device_key == ("meta_quest_camera", "192.0.2.10")


import httpx


def _status_only_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(
                200,
                json={
                    "recording": False,
                    "session_id": None,
                    "last_preview_capture_ns": 0,
                    "left_camera_ready": True,
                    "right_camera_ready": True,
                    "storage_free_bytes": 1_000_000_000,
                },
            )
        if request.url.path.startswith("/preview/"):
            # Return a tiny valid MJPEG body (no frames) so the consumer blocks.
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "multipart/x-mixed-replace; boundary=syncfield"
                },
                content=b"",
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class TestConnectDisconnect:
    def test_connect_runs_status_probe_and_starts_preview(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="test",
            output_dir=tmp_path,
            _transport=_status_only_transport(),  # test-only injection
        )
        stream.connect()
        assert stream.is_connected is True
        stream.disconnect()
        assert stream.is_connected is False

    def test_connect_raises_when_quest_unreachable(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable")

        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="test",
            output_dir=tmp_path,
            _transport=httpx.MockTransport(handler),
        )
        with pytest.raises(httpx.ConnectError):
            stream.connect()
