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

    def test_jitter_reported_when_enough_frames(
        self, mock_av_generous, tmp_path
    ):
        """After recording 20+ frames, jitter p95/p99 are populated."""
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        stream = UVCWebcamStream(
            "cam", device_index=0, output_dir=tmp_path, fps=30.0
        )
        stream.prepare()
        stream.connect()
        stream.start_recording(_clock())
        # Need enough recorded frames for the >=20 sample threshold.
        # mock_av_generous paces at 1ms/frame, so 100ms should yield ~90.
        time.sleep(0.15)
        report = stream.stop_recording()
        stream.disconnect()

        assert report.jitter_p95_ns is not None
        assert report.jitter_p99_ns is not None
        assert report.jitter_p99_ns >= report.jitter_p95_ns
        # Sanity: jitter should be on the order of pace_seconds (1ms = 1_000_000 ns)
        # — allow generous bounds for CI load.
        assert 0 < report.jitter_p95_ns < 100_000_000  # < 100ms


class TestDecoderResilience:
    """The capture loop must tolerate transient decoder errors.

    AVFoundation on macOS raises ``BlockingIOError`` (EAGAIN, errno 35)
    during camera warmup and occasionally between frames. Linux V4L2
    surfaces the same under EAGAIN=11. Interrupted syscalls (EINTR=4)
    fall in the same bucket. None should kill the capture thread.
    """

    def test_blocking_io_error_does_not_kill_loop(
        self, mock_av_generous, tmp_path
    ):
        """Inject EAGAIN into the decode iterator; loop must keep going."""
        import numpy as np
        from unittest.mock import MagicMock

        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        # A generator that ``raise``s without ever yielding becomes
        # dead after the first next(), so we use an iterator class
        # that keeps state across next() calls: first 3 calls raise
        # EAGAIN, subsequent 50 yield real BGR frames.
        class FlakyIter:
            def __init__(self) -> None:
                self._eagain_left = 3
                self._i = 0

            def __iter__(self):
                return self

            def __next__(self):
                if self._eagain_left > 0:
                    self._eagain_left -= 1
                    raise BlockingIOError(
                        35, "Resource temporarily unavailable", "0"
                    )
                if self._i >= 50:
                    raise StopIteration
                time.sleep(0.001)
                frame = MagicMock(name=f"Frame-{self._i}")
                frame.to_ndarray = MagicMock(
                    return_value=np.full(
                        (48, 64, 3), self._i % 256, dtype=np.uint8
                    )
                )
                self._i += 1
                return frame

        mock_av_generous.input_container.decode = MagicMock(
            return_value=FlakyIter()
        )

        stream = UVCWebcamStream(
            "cam", device_index=0, output_dir=tmp_path, fps=30.0
        )
        stream.prepare()
        stream.connect()
        stream.start_recording(_clock())
        time.sleep(0.1)
        report = stream.stop_recording()
        stream.disconnect()

        assert report.frame_count >= 1
        assert not any(
            "BlockingIOError" in (h.detail or "")
            for h in report.health_events
        )

    def test_fatal_os_error_still_ends_loop(
        self, mock_av_generous, tmp_path
    ):
        """Non-transient OSError (e.g. ENODEV=19) still emits + exits."""
        from unittest.mock import MagicMock

        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        class FatalIter:
            def __iter__(self):
                return self

            def __next__(self):
                raise OSError(19, "No such device", "99")

        mock_av_generous.input_container.decode = MagicMock(
            return_value=FatalIter()
        )

        stream = UVCWebcamStream(
            "cam", device_index=99, output_dir=tmp_path, fps=30.0
        )
        stream.prepare()
        stream.connect()
        time.sleep(0.05)
        stream.disconnect()

        collected = stream._collected_health  # noqa: SLF001
        assert any(
            "No such device" in (h.detail or "") for h in collected
        ), f"expected fatal OSError in health events, got {collected!r}"
