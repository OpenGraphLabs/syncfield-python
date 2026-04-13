"""Unit tests for UVCWebcamStream using a mocked PyAV module."""

from __future__ import annotations

import importlib
import sys
import time

import pytest

from syncfield.clock import SessionClock
from syncfield.types import SyncPoint


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


# ---------------------------------------------------------------------------
# Basic SPI coverage
# ---------------------------------------------------------------------------


def test_capabilities(mock_av, tmp_path):
    from syncfield.adapters.uvc_webcam import UVCWebcamStream

    stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
    assert stream.capabilities.produces_file is True
    assert stream.capabilities.provides_audio_track is False
    assert stream.kind == "video"


def test_prepare_opens_pyav_input(mock_av, tmp_path):
    from syncfield.adapters.uvc_webcam import UVCWebcamStream

    stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
    stream.prepare()

    # av.open was called with the input URL (macOS or Linux depending on
    # the runner), NOT with mode="w".
    input_calls = [
        c for c in mock_av.av.open.call_args_list
        if c.kwargs.get("mode") != "w"
    ]
    assert len(input_calls) == 1


def test_start_stop_produces_file_path_in_report(mock_av, tmp_path):
    from syncfield.adapters.uvc_webcam import UVCWebcamStream

    stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
    stream.prepare()
    stream.start(_clock())
    time.sleep(0.1)  # let the background thread drain the decode iterator
    report = stream.stop()
    assert report.status == "completed"
    assert report.file_path is not None
    assert report.frame_count >= 1


def test_av_missing_raises_clear_install_hint(monkeypatch):
    """If PyAV is not installed, importing the video-encoder module
    (and transitively the UVC adapter) raises a hint mentioning the
    ``syncfield[uvc]`` extra.
    """
    monkeypatch.setitem(sys.modules, "av", None)
    sys.modules.pop("syncfield.adapters._video_encoder", None)
    sys.modules.pop("syncfield.adapters.uvc_webcam", None)
    import syncfield.adapters as _adapters_pkg
    monkeypatch.delattr(_adapters_pkg, "_video_encoder", raising=False)
    monkeypatch.delattr(_adapters_pkg, "uvc_webcam", raising=False)
    with pytest.raises(ImportError, match=r"syncfield\[uvc\]"):
        importlib.import_module("syncfield.adapters.uvc_webcam")


# ---------------------------------------------------------------------------
# 4-phase lifecycle — live preview before recording
# ---------------------------------------------------------------------------


class TestFourPhaseLifecycle:
    """UVCWebcamStream must support live preview in CONNECTED state.

    The 4-phase lifecycle is what the viewer uses: ``connect()`` runs
    the capture thread in preview-only mode so ``latest_frame``
    populates before the user clicks Record; ``start_recording()``
    then flips the recording flag and opens the encoder without
    respawning the thread.
    """

    def test_connect_starts_preview_without_writing(
        self, mock_av_generous, tmp_path
    ):
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        time.sleep(0.1)  # let the thread read a few mocked frames
        try:
            # Encoder was never constructed — preview phase doesn't write.
            assert stream._encoder is None  # noqa: SLF001
            # No SampleEvent emissions, no advanced frame counter.
            assert stream._frame_count == 0  # noqa: SLF001
            # But latest_frame IS populated so the viewer card can
            # render the live thumbnail.
            assert stream.latest_frame is not None
        finally:
            stream.disconnect()

    def test_start_recording_flips_to_writing(self, mock_av_generous, tmp_path):
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
            # With pace_seconds=0.001 and 0.1s recording window we expect roughly
            # ~100 frames. Assert a lower bound that's meaningful (>5) but a loose
            # upper bound that tolerates CI jitter.
            assert 5 <= report.frame_count <= 10_000
            assert report.file_path is not None
            # Stream stays connected after stop_recording — the thread
            # is still alive so the preview continues.
            assert stream._thread is not None  # noqa: SLF001
            assert stream._thread.is_alive()  # noqa: SLF001
            # Encoder stream was actually called to encode at least once.
            assert mock_av_generous.output_stream.encode.called
        finally:
            stream.disconnect()

    def test_disconnect_stops_capture_thread(self, mock_av_generous, tmp_path):
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        time.sleep(0.05)
        stream.disconnect()
        assert stream._thread is None  # noqa: SLF001

    def test_connect_is_idempotent(self, mock_av_generous, tmp_path):
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
