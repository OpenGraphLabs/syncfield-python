"""Unit tests for RecordingFilePuller."""

from __future__ import annotations

import httpx
import pytest

from syncfield.adapters.meta_quest_camera.file_puller import (
    RecordingFilePuller,
    RecordingArtifacts,
)
from syncfield.adapters.meta_quest_camera.http_client import QuestHttpClient


def _router():
    files = {
        "/recording/files/left": b"LEFT_MP4",
        "/recording/files/right": b"RIGHT_MP4",
        "/recording/timestamps/left":
            b'{"frame_number":0,"capture_ns":1}\n{"frame_number":1,"capture_ns":2}\n',
        "/recording/timestamps/right":
            b'{"frame_number":0,"capture_ns":1}\n{"frame_number":1,"capture_ns":2}\n',
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = files.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"Content-Length": str(len(body))}, content=body
        )

    return httpx.MockTransport(handler)


class TestRecordingFilePuller:
    def test_pulls_all_four_artifacts(self, tmp_path):
        client = QuestHttpClient(host="test", port=14045, transport=_router())
        puller = RecordingFilePuller(
            client=client, stream_id="quest_cam", output_dir=tmp_path
        )
        artifacts = puller.pull_all()

        assert isinstance(artifacts, RecordingArtifacts)
        assert artifacts.left_mp4.read_bytes() == b"LEFT_MP4"
        assert artifacts.right_mp4.read_bytes() == b"RIGHT_MP4"
        assert artifacts.left_timestamps.exists()
        assert artifacts.right_timestamps.exists()

        # File naming matches the adapter's documented output layout.
        assert artifacts.left_mp4.name == "quest_cam_left.mp4"
        assert artifacts.right_mp4.name == "quest_cam_right.mp4"
        assert artifacts.left_timestamps.name == "quest_cam_left.timestamps.jsonl"
        assert artifacts.right_timestamps.name == "quest_cam_right.timestamps.jsonl"
