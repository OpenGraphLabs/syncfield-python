"""Unit tests for the streaming MetaQuestCameraStream adapter.

The adapter sits on top of three collaborators that have their own
focused tests (preview, mp4_writer, http_client). These tests cover
the surface the orchestrator actually drives:

* identity / capabilities
* connect/disconnect lifecycle (status probe, preview start)
* start/stop recording without exercising real network I/O — the
  ``StreamingVideoRecorder`` integration is verified by feeding raw
  JPEG frames into the consumer's sink directly.
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

from syncfield.adapters.meta_quest_camera import MetaQuestCameraStream
from syncfield.adapters.meta_quest_camera.preview import MjpegFrame
from syncfield.clock import SessionClock, SyncPoint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status_only_transport() -> httpx.MockTransport:
    """Transport that satisfies /status and stalls preview pulls.

    Used for tests that don't care about frame delivery — preview
    consumers stay blocked reading an empty body so latest_frame
    stays None and no sink callbacks fire.
    """

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
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "multipart/x-mixed-replace; boundary=syncfield"
                },
                content=b"",
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _make_jpeg(width: int = 64, height: int = 64, color=(120, 50, 200)) -> bytes:
    """Build a minimal valid JPEG so PyAV's MJPEG packetiser is happy."""
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Connect / disconnect
# ---------------------------------------------------------------------------


class TestConnectDisconnect:
    def test_connect_runs_status_probe_and_starts_preview(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="test",
            output_dir=tmp_path,
            _transport=_status_only_transport(),
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


# ---------------------------------------------------------------------------
# Recording — exercises the sink + StreamingVideoRecorder integration
# ---------------------------------------------------------------------------


class TestRecording:
    def test_start_recording_requires_connect(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam", quest_host="test", output_dir=tmp_path,
        )
        clock = SessionClock(sync_point=SyncPoint.create_now("test_host"))
        with pytest.raises(RuntimeError):
            stream.start_recording(clock)

    def test_start_then_stop_with_no_frames_marks_failed(self, tmp_path):
        """No frames flowed through the sink → stop returns failed status."""
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="test",
            output_dir=tmp_path,
            _transport=_status_only_transport(),
        )
        stream.connect()
        clock = SessionClock(sync_point=SyncPoint.create_now("test_host"))
        stream.start_recording(clock)
        report = stream.stop_recording()
        stream.disconnect()
        assert report.status == "failed"
        assert "no frames" in (report.error or "")

    def test_recording_writes_mp4_and_timestamps_on_frames(self, tmp_path):
        """Push a handful of JPEG frames into both sinks → both eyes
        produce valid mp4 + jsonl, status==completed."""
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="test",
            output_dir=tmp_path,
            _transport=_status_only_transport(),
            resolution=(64, 64),
        )
        stream.connect()
        clock = SessionClock(sync_point=SyncPoint.create_now("test_host"))
        stream.start_recording(clock)

        # Reach into the consumers and fire their registered sinks
        # directly — bypasses the network thread so the test stays
        # deterministic. The sink is the same callable the consumer
        # would invoke per real MJPEG frame.
        jpeg = _make_jpeg()
        base_ns = time.monotonic_ns()
        for i in range(5):
            host_ns = base_ns + i * 33_333_333
            quest_ns = host_ns - 1_000_000  # arbitrary delta
            stream._preview_left._frame_sink(
                MjpegFrame(jpeg_bytes=jpeg, capture_ns=host_ns, quest_native_ns=quest_ns)
            )
            stream._preview_right._frame_sink(
                MjpegFrame(jpeg_bytes=jpeg, capture_ns=host_ns, quest_native_ns=quest_ns)
            )

        report = stream.stop_recording()
        stream.disconnect()

        assert report.status == "completed", report.error
        assert report.frame_count == 5
        assert (tmp_path / "quest_cam_left.mp4").stat().st_size > 0
        assert (tmp_path / "quest_cam_right.mp4").stat().st_size > 0
        ts_left = (tmp_path / "quest_cam_left.timestamps.jsonl").read_text()
        ts_right = (tmp_path / "quest_cam_right.timestamps.jsonl").read_text()
        assert ts_left.count("\n") == 5
        assert ts_right.count("\n") == 5
        # Each line must carry both timestamps.
        assert "quest_native_ns" in ts_left

    def test_partial_status_when_only_one_eye_received_frames(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="test",
            output_dir=tmp_path,
            _transport=_status_only_transport(),
            resolution=(64, 64),
        )
        stream.connect()
        clock = SessionClock(sync_point=SyncPoint.create_now("test_host"))
        stream.start_recording(clock)

        jpeg = _make_jpeg()
        # Only the left eye gets frames (right stays at zero).
        for i in range(3):
            stream._preview_left._frame_sink(
                MjpegFrame(jpeg_bytes=jpeg, capture_ns=time.monotonic_ns() + i, quest_native_ns=None)
            )

        report = stream.stop_recording()
        stream.disconnect()
        assert report.status == "partial"
        assert "single-eye" in (report.error or "") or "right=0" in (report.error or "")

    def test_double_start_recording_raises(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="test",
            output_dir=tmp_path,
            _transport=_status_only_transport(),
        )
        stream.connect()
        clock = SessionClock(sync_point=SyncPoint.create_now("test_host"))
        stream.start_recording(clock)
        try:
            with pytest.raises(RuntimeError, match="already in progress"):
                stream.start_recording(clock)
        finally:
            stream.stop_recording()
            stream.disconnect()


# ---------------------------------------------------------------------------
# Viewer-facing properties
# ---------------------------------------------------------------------------


class TestLatestFrame:
    def test_latest_frame_none_before_connect(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam", quest_host="test", output_dir=tmp_path,
        )
        assert stream.latest_frame_left is None
        assert stream.latest_frame_right is None
        assert stream.latest_frame is None

    def test_latest_frame_reads_from_preview_consumers(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam", quest_host="test", output_dir=tmp_path,
            _transport=_status_only_transport(),
        )
        stream.connect()
        # Empty preview body in the fixture → consumers never produce
        # a decoded frame, so the slot stays None.
        assert stream.latest_frame_left is None
        assert stream.latest_frame_right is None
        stream.disconnect()
