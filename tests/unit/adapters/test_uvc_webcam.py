"""Unit tests for UVCWebcamStream using a mocked PyAV module."""

from __future__ import annotations

import importlib
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from syncfield.clock import SessionClock
from syncfield.types import SyncPoint


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


def _make_frame(i: int) -> MagicMock:
    """Build a fake PyAV VideoFrame whose ``to_ndarray`` returns BGR24."""
    frame = MagicMock(name=f"Frame-{i}")
    frame.to_ndarray = MagicMock(
        return_value=np.full((48, 64, 3), i % 256, dtype=np.uint8)
    )
    return frame


def _build_fake_av(
    frame_budget: int = 3, pace_seconds: float = 0.0
) -> tuple[SimpleNamespace, MagicMock, MagicMock]:
    """Build a fake ``av`` module and return (av, input_container, output_stream).

    The input container's ``decode(video=0)`` yields ``frame_budget`` fake
    frames then stops. When ``pace_seconds`` is > 0 the generator sleeps
    between frames so the background capture thread doesn't exhaust the
    iterator faster than the test can interact with the stream. The
    output container's ``add_stream`` returns a stream whose ``encode``
    yields one packet per call (empty list on flush with ``None``).
    """

    def _frame_gen():
        for i in range(frame_budget):
            if pace_seconds > 0:
                time.sleep(pace_seconds)
            yield _make_frame(i)

    input_container = MagicMock(name="InputContainer")
    input_container.decode = MagicMock(return_value=_frame_gen())

    output_stream = MagicMock(name="VideoStream")
    packet = MagicMock(name="Packet")
    output_stream.encode = MagicMock(
        side_effect=lambda frame: [packet] if frame is not None else []
    )
    output_container = MagicMock(name="OutputContainer")
    output_container.add_stream = MagicMock(return_value=output_stream)

    def _av_open(url, *args, **kwargs):  # noqa: ANN001 - MagicMock signature
        if kwargs.get("mode") == "w":
            return output_container
        return input_container

    av = SimpleNamespace()
    av.open = MagicMock(side_effect=_av_open)
    av.VideoFrame = SimpleNamespace(
        from_ndarray=MagicMock(return_value=MagicMock(name="OutFrame"))
    )
    av.codec = SimpleNamespace(
        Codec=MagicMock(side_effect=lambda n, m: SimpleNamespace(name=n))
    )
    return av, input_container, output_stream


@pytest.fixture
def mock_av(monkeypatch):
    """Patch ``sys.modules['av']`` with a fake and force-reimport the encoder."""
    av, input_container, output_stream = _build_fake_av(frame_budget=3)
    monkeypatch.setitem(sys.modules, "av", av)
    # Clear cached imports so the adapter and encoder bind to the fake av.
    sys.modules.pop("syncfield.adapters._video_encoder", None)
    sys.modules.pop("syncfield.adapters.uvc_webcam", None)
    # Drop parent-package cached attribute so ``from syncfield.adapters import _video_encoder`` reloads.
    import syncfield.adapters as _adapters_pkg
    monkeypatch.delattr(_adapters_pkg, "_video_encoder", raising=False)
    monkeypatch.delattr(_adapters_pkg, "uvc_webcam", raising=False)

    importlib.import_module("syncfield.adapters._video_encoder")
    importlib.import_module("syncfield.adapters.uvc_webcam")
    yield SimpleNamespace(
        av=av,
        input_container=input_container,
        output_stream=output_stream,
    )
    sys.modules.pop("syncfield.adapters.uvc_webcam", None)
    sys.modules.pop("syncfield.adapters._video_encoder", None)


@pytest.fixture
def mock_av_generous(monkeypatch):
    """Same as ``mock_av`` but yields effectively unlimited frames so the
    capture thread can run through both preview and recording phases
    without exhausting the mocked ``decode()`` iterator.
    """
    # Pace the generator so the background thread doesn't exhaust the
    # iterator during the test's ``time.sleep(...)`` windows — without
    # pacing, 10_000 MagicMock frames complete in microseconds and the
    # capture thread exits before ``start_recording()`` is called.
    av, input_container, output_stream = _build_fake_av(
        frame_budget=10_000, pace_seconds=0.001
    )
    monkeypatch.setitem(sys.modules, "av", av)
    sys.modules.pop("syncfield.adapters._video_encoder", None)
    sys.modules.pop("syncfield.adapters.uvc_webcam", None)
    import syncfield.adapters as _adapters_pkg
    monkeypatch.delattr(_adapters_pkg, "_video_encoder", raising=False)
    monkeypatch.delattr(_adapters_pkg, "uvc_webcam", raising=False)
    importlib.import_module("syncfield.adapters._video_encoder")
    importlib.import_module("syncfield.adapters.uvc_webcam")
    yield SimpleNamespace(
        av=av,
        input_container=input_container,
        output_stream=output_stream,
    )
    sys.modules.pop("syncfield.adapters.uvc_webcam", None)
    sys.modules.pop("syncfield.adapters._video_encoder", None)


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
            assert report.frame_count >= 1  # recording did
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
