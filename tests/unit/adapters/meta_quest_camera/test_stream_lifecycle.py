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
        if request.url.path == "/clock/sync":
            return httpx.Response(200, json={"ok": True})
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

    def test_recording_writes_single_mp4_on_frames(self, tmp_path):
        """Push a handful of JPEG frames into the sink → single mp4
        produced, status==completed, NO sidecar jsonl written."""
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

        # Reach into the consumer and fire its registered sink directly —
        # bypasses the network thread so the test stays deterministic.
        jpeg = _make_jpeg()
        base_ns = time.monotonic_ns()
        for i in range(5):
            host_ns = base_ns + i * 33_333_333
            quest_ns = host_ns - 1_000_000  # arbitrary delta
            stream._preview._frame_sink(
                MjpegFrame(jpeg_bytes=jpeg, capture_ns=host_ns, quest_native_ns=quest_ns)
            )

        report = stream.stop_recording()
        stream.disconnect()

        assert report.status == "completed", report.error
        assert report.frame_count == 5
        # Single MP4, no _left/_right suffix
        assert (tmp_path / "quest_cam.mp4").stat().st_size > 0
        assert not (tmp_path / "quest_cam_left.mp4").exists()
        assert not (tmp_path / "quest_cam_right.mp4").exists()
        # Recorder no longer writes its own jsonl — orchestrator's
        # auto-jsonl path handles per-frame timestamps.
        assert not (tmp_path / "quest_cam.timestamps.jsonl").exists()

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
        assert stream.latest_frame is None

    def test_latest_frame_reads_from_preview_consumer(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam", quest_host="test", output_dir=tmp_path,
            _transport=_status_only_transport(),
        )
        stream.connect()
        # Empty preview body in the fixture → consumer never produces
        # a decoded frame, so the slot stays None.
        assert stream.latest_frame is None
        stream.disconnect()
