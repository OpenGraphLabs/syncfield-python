"""Unit tests for UVCWebcamStream using a mocked cv2 module."""

from __future__ import annotations

import importlib
import sys
import time
from unittest.mock import MagicMock

import pytest

from syncfield.clock import SessionClock
from syncfield.types import SyncPoint


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


def _build_fake_cv2(frame_budget: int = 3) -> MagicMock:
    """Return a MagicMock that looks enough like cv2 for the adapter."""
    fake = MagicMock()
    cap = MagicMock()
    counter = {"n": 0}

    def fake_read():
        counter["n"] += 1
        if counter["n"] <= frame_budget:
            return True, MagicMock(shape=(480, 640, 3))
        return False, None

    cap.read.side_effect = fake_read
    cap.isOpened.return_value = True

    def fake_get(prop):
        if prop == fake.CAP_PROP_FPS:
            return 30.0
        if prop == fake.CAP_PROP_FRAME_WIDTH:
            return 640
        if prop == fake.CAP_PROP_FRAME_HEIGHT:
            return 480
        return 0.0

    cap.get.side_effect = fake_get
    fake.VideoCapture.return_value = cap
    fake.CAP_PROP_FPS = "CAP_PROP_FPS"
    fake.CAP_PROP_FRAME_WIDTH = "CAP_PROP_FRAME_WIDTH"
    fake.CAP_PROP_FRAME_HEIGHT = "CAP_PROP_FRAME_HEIGHT"
    fake.VideoWriter_fourcc = lambda *args: 0
    fake.VideoWriter.return_value = MagicMock()
    return fake


@pytest.fixture
def mock_cv2(monkeypatch):
    fake = _build_fake_cv2()
    monkeypatch.setitem(sys.modules, "cv2", fake)
    # Force re-import so the adapter binds to the fake module
    sys.modules.pop("syncfield.adapters.uvc_webcam", None)
    importlib.import_module("syncfield.adapters.uvc_webcam")
    yield fake
    sys.modules.pop("syncfield.adapters.uvc_webcam", None)


def test_capabilities(mock_cv2, tmp_path):
    from syncfield.adapters.uvc_webcam import UVCWebcamStream
    stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
    assert stream.capabilities.produces_file is True
    assert stream.capabilities.provides_audio_track is False
    assert stream.kind == "video"


def test_prepare_opens_device(mock_cv2, tmp_path):
    from syncfield.adapters.uvc_webcam import UVCWebcamStream
    stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
    stream.prepare()
    mock_cv2.VideoCapture.assert_called_once_with(0)


def test_prepare_raises_when_device_fails_to_open(mock_cv2, tmp_path):
    mock_cv2.VideoCapture.return_value.isOpened.return_value = False
    from syncfield.adapters.uvc_webcam import UVCWebcamStream
    stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
    with pytest.raises(RuntimeError, match="VideoCapture"):
        stream.prepare()


def test_start_stop_produces_file_path_in_report(mock_cv2, tmp_path):
    from syncfield.adapters.uvc_webcam import UVCWebcamStream
    stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
    stream.prepare()
    stream.start(_clock())
    # Let the background thread read the mocked frames
    time.sleep(0.1)
    report = stream.stop()
    assert report.status == "completed"
    assert report.file_path is not None
    assert report.frame_count >= 1


def test_cv2_missing_raises_clear_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", None)
    sys.modules.pop("syncfield.adapters.uvc_webcam", None)
    with pytest.raises(ImportError, match=r"syncfield\[uvc\]"):
        importlib.import_module("syncfield.adapters.uvc_webcam")


# ---------------------------------------------------------------------------
# 4-phase lifecycle — live preview before recording
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_cv2_generous(monkeypatch):
    """Like ``mock_cv2`` but with a large frame budget so the capture
    thread can run through a preview phase AND a recording phase
    without exhausting the mocked ``read()`` side-effect.
    """
    fake = _build_fake_cv2(frame_budget=10_000)
    monkeypatch.setitem(sys.modules, "cv2", fake)
    sys.modules.pop("syncfield.adapters.uvc_webcam", None)
    importlib.import_module("syncfield.adapters.uvc_webcam")
    yield fake
    sys.modules.pop("syncfield.adapters.uvc_webcam", None)


class TestFourPhaseLifecycle:
    """UVCWebcamStream must support live preview in CONNECTED state.

    The 4-phase lifecycle is what the viewer uses: ``connect()`` runs
    the capture thread in preview-only mode so ``latest_frame``
    populates before the user clicks Record; ``start_recording()``
    then flips the recording flag and opens the writer without
    respawning the thread.
    """

    def test_connect_starts_preview_without_writing(self, mock_cv2_generous, tmp_path):
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        time.sleep(0.1)  # let the thread read a few mocked frames
        try:
            # Writer was never constructed — preview phase doesn't write.
            assert stream._writer is None  # noqa: SLF001
            # No SampleEvent emissions, no advanced frame counter.
            assert stream._frame_count == 0  # noqa: SLF001
            # But latest_frame IS populated so the viewer card can
            # render the live thumbnail.
            assert stream.latest_frame is not None
        finally:
            stream.disconnect()

    def test_start_recording_flips_to_writing(self, mock_cv2_generous, tmp_path):
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        time.sleep(0.05)  # preview phase
        frames_before = stream._frame_count  # noqa: SLF001

        stream.start_recording(_clock())
        time.sleep(0.1)  # recording phase

        report = stream.stop_recording()
        try:
            assert frames_before == 0  # preview didn't advance the counter
            assert report.frame_count >= 1  # recording did
            assert report.file_path is not None
            # Stream stays connected after stop_recording — the thread
            # is still alive so the preview continues.
            assert stream._thread is not None  # noqa: SLF001
            assert stream._thread.is_alive()  # noqa: SLF001
        finally:
            stream.disconnect()

    def test_disconnect_stops_capture_thread(self, mock_cv2_generous, tmp_path):
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        time.sleep(0.05)
        stream.disconnect()
        # Thread handle is cleared and the OS thread has joined.
        assert stream._thread is None  # noqa: SLF001

    def test_connect_is_idempotent(self, mock_cv2_generous, tmp_path):
        """Calling connect() twice must not spawn a second thread."""
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        first_thread = stream._thread  # noqa: SLF001
        stream.connect()  # second call is a no-op
        try:
            assert stream._thread is first_thread  # noqa: SLF001
        finally:
            stream.disconnect()
