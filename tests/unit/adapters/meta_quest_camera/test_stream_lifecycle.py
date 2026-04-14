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


import json
from syncfield.clock import SessionClock, SyncPoint


def _full_quest_transport(left_mp4=b"LEFT_MP4", right_mp4=b"RIGHT_MP4"):
    state = {"recording": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/status":
            return httpx.Response(200, json={
                "recording": state["recording"], "session_id": None,
                "last_preview_capture_ns": 0,
                "left_camera_ready": True, "right_camera_ready": True,
                "storage_free_bytes": 1_000_000_000,
            })
        if path.startswith("/preview/"):
            return httpx.Response(200, headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=syncfield"
            }, content=b"")
        if path == "/recording/start":
            state["recording"] = True
            return httpx.Response(200, json={
                "session_id": "ep_x", "quest_mono_ns_at_start": 0,
                "delta_ns": 0, "started": True,
            })
        if path == "/recording/stop":
            state["recording"] = False
            return httpx.Response(200, json={
                "session_id": "ep_x",
                "left":  {"frame_count": 2, "bytes": len(left_mp4),  "last_capture_ns": 2},
                "right": {"frame_count": 2, "bytes": len(right_mp4), "last_capture_ns": 2},
                "duration_s": 0.1,
            })
        if path == "/recording/files/left":
            return httpx.Response(200, headers={"Content-Length": str(len(left_mp4))}, content=left_mp4)
        if path == "/recording/files/right":
            return httpx.Response(200, headers={"Content-Length": str(len(right_mp4))}, content=right_mp4)
        if path == "/recording/timestamps/left" or path == "/recording/timestamps/right":
            body = (
                b'{"frame_number":0,"capture_ns":1}\n'
                b'{"frame_number":1,"capture_ns":2}\n'
            )
            return httpx.Response(200, headers={"Content-Length": str(len(body))}, content=body)
        if path == "/recording/files" and request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class TestRecordingRoundtrip:
    def test_full_recording_lifecycle(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="test",
            output_dir=tmp_path,
            _transport=_full_quest_transport(),
        )
        stream.connect()
        clock = SessionClock(sync_point=SyncPoint.create_now("test_host"))

        stream.start_recording(clock)
        report = stream.stop_recording()
        stream.disconnect()

        assert report.status == "completed"
        assert (tmp_path / "quest_cam_left.mp4").read_bytes() == b"LEFT_MP4"
        assert (tmp_path / "quest_cam_right.mp4").read_bytes() == b"RIGHT_MP4"
        assert (tmp_path / "quest_cam_left.timestamps.jsonl").exists()
        assert (tmp_path / "quest_cam_right.timestamps.jsonl").exists()


class TestSizeMismatch:
    def test_partial_status_when_size_mismatch(self, tmp_path):
        """When /stop says left.bytes=9999 but actual file is 8 bytes, status=partial."""
        # left_mp4 body is b"LEFT_MP4" (8 bytes), but /stop will claim bytes=9999
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="test",
            output_dir=tmp_path,
            _transport=_full_quest_transport(left_mp4=b"LEFT_MP4", right_mp4=b"RIGHT_MP4"),
        )

        # Build a transport that overrides /recording/stop to return wrong byte count
        def _mismatched_transport():
            state = {"recording": False}

            def handler(request: httpx.Request) -> httpx.Response:
                path = request.url.path
                if path == "/status":
                    return httpx.Response(200, json={
                        "recording": state["recording"], "session_id": None,
                        "last_preview_capture_ns": 0,
                        "left_camera_ready": True, "right_camera_ready": True,
                        "storage_free_bytes": 1_000_000_000,
                    })
                if path.startswith("/preview/"):
                    return httpx.Response(200, headers={
                        "Content-Type": "multipart/x-mixed-replace; boundary=syncfield"
                    }, content=b"")
                if path == "/recording/start":
                    state["recording"] = True
                    return httpx.Response(200, json={
                        "session_id": "ep_x", "quest_mono_ns_at_start": 0,
                        "delta_ns": 0, "started": True,
                    })
                if path == "/recording/stop":
                    state["recording"] = False
                    return httpx.Response(200, json={
                        "session_id": "ep_x",
                        # 9999 != 8 bytes of b"LEFT_MP4"
                        "left":  {"frame_count": 2, "bytes": 9999, "last_capture_ns": 2},
                        "right": {"frame_count": 2, "bytes": len(b"RIGHT_MP4"), "last_capture_ns": 2},
                        "duration_s": 0.1,
                    })
                if path == "/recording/files/left":
                    body = b"LEFT_MP4"
                    return httpx.Response(200, headers={"Content-Length": str(len(body))}, content=body)
                if path == "/recording/files/right":
                    body = b"RIGHT_MP4"
                    return httpx.Response(200, headers={"Content-Length": str(len(body))}, content=body)
                if path == "/recording/timestamps/left" or path == "/recording/timestamps/right":
                    body = (
                        b'{"frame_number":0,"capture_ns":1}\n'
                        b'{"frame_number":1,"capture_ns":2}\n'
                    )
                    return httpx.Response(200, headers={"Content-Length": str(len(body))}, content=body)
                if path == "/recording/files":
                    # DELETE — best-effort cleanup
                    return httpx.Response(204)
                return httpx.Response(404)

            return httpx.MockTransport(handler)

        stream2 = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="test",
            output_dir=tmp_path / "mismatch",
            _transport=_mismatched_transport(),
        )
        stream2.connect()
        clock = SessionClock(sync_point=SyncPoint.create_now("test_host"))
        stream2.start_recording(clock)
        report = stream2.stop_recording()
        stream2.disconnect()

        assert report.status == "partial"
        assert report.error is not None
        assert "size" in report.error.lower() or "bytes" in report.error.lower()


class TestLatestFrame:
    def test_latest_frame_none_before_connect(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam", quest_host="test", output_dir=tmp_path,
        )
        assert stream.latest_frame_left is None
        assert stream.latest_frame_right is None

    def test_latest_frame_reads_from_preview_consumers(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam", quest_host="test", output_dir=tmp_path,
            _transport=_status_only_transport(),
        )
        stream.connect()
        # Consumers returned empty body in the fixture, so latest_frame stays None.
        assert stream.latest_frame_left is None
        assert stream.latest_frame_right is None
        stream.disconnect()
