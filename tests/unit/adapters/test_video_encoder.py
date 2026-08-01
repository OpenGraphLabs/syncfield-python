"""Unit tests for VideoEncoder — the shared PyAV MP4 writer.

The real ``av`` module is replaced with a fake so these tests do not
depend on a working FFmpeg build. We only assert that:

* The encoder opens an output container with the right path and format.
* It adds one video stream with the requested width, height, fps, pixel
  format and a usable codec (h264_videotoolbox on mac, libx264 elsewhere).
* ``write(frame)`` encodes and muxes one packet per call.
* ``close()`` flushes the encoder and closes the container exactly once.
* Double ``close()`` is idempotent (no double-flush, no exception).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest


@pytest.fixture
def fake_av(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Install a fake ``av`` module that records every interaction."""
    container = MagicMock(name="OutputContainer")
    stream = MagicMock(name="VideoStream")
    stream.width = 0
    stream.height = 0
    stream.pix_fmt = "yuv420p"
    container.add_stream.return_value = stream

    packet = MagicMock(name="Packet")
    stream.encode = MagicMock(
        side_effect=lambda frame: [packet] if frame is not None else []
    )

    av = SimpleNamespace()
    av.open = MagicMock(name="av.open", return_value=container)

    def _from_ndarray(arr: np.ndarray, format: str) -> MagicMock:
        frame = MagicMock(name="VideoFrame")
        frame.to_ndarray = lambda format="bgr24": arr
        frame._source_format = format
        return frame

    video_frame = SimpleNamespace(from_ndarray=MagicMock(side_effect=_from_ndarray))
    av.VideoFrame = video_frame

    def _codec(name: str, mode: str) -> SimpleNamespace:
        if name in {"h264_videotoolbox", "libx264"}:
            return SimpleNamespace(name=name)
        raise ValueError(f"unknown codec {name}")

    av.codec = SimpleNamespace(Codec=MagicMock(side_effect=_codec))

    # Remove any cached import of the module under test so it picks up
    # the fake ``av`` when re-imported. Also drop the attribute on the
    # parent package, otherwise ``from syncfield.adapters import
    # _video_encoder`` would resolve via the cached parent attribute and
    # skip the reload.
    sys.modules.pop("syncfield.adapters._video_encoder", None)
    parent = sys.modules.get("syncfield.adapters")
    if parent is not None and hasattr(parent, "_video_encoder"):
        monkeypatch.delattr(parent, "_video_encoder", raising=False)
    monkeypatch.setitem(sys.modules, "av", av)
    return SimpleNamespace(av=av, container=container, stream=stream, packet=packet)


def test_open_creates_output_container(
    tmp_path: Path, fake_av: SimpleNamespace
) -> None:
    from syncfield.adapters._video_encoder import VideoEncoder

    out = tmp_path / "clip.mp4"
    enc = VideoEncoder.open(out, width=1280, height=720, fps=30.0)

    fake_av.av.open.assert_called_once()
    args, kwargs = fake_av.av.open.call_args
    assert args[0] == str(out)
    assert kwargs.get("mode") == "w"

    fake_av.container.add_stream.assert_called_once()
    stream_args, stream_kwargs = fake_av.container.add_stream.call_args
    assert stream_args[0] in {"h264_videotoolbox", "libx264"}
    assert stream_kwargs.get("rate") == 30
    assert fake_av.stream.width == 1280
    assert fake_av.stream.height == 720

    enc.close()


def test_write_encodes_and_muxes_one_frame(
    tmp_path: Path, fake_av: SimpleNamespace
) -> None:
    from syncfield.adapters._video_encoder import VideoEncoder

    enc = VideoEncoder.open(tmp_path / "clip.mp4", width=64, height=48, fps=30.0)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)

    enc.write(frame)

    fake_av.av.VideoFrame.from_ndarray.assert_called_once()
    fake_av.stream.encode.assert_called()
    fake_av.container.mux.assert_called_once_with(fake_av.packet)

    enc.close()


def test_close_is_idempotent(tmp_path: Path, fake_av: SimpleNamespace) -> None:
    from syncfield.adapters._video_encoder import VideoEncoder

    enc = VideoEncoder.open(tmp_path / "clip.mp4", width=64, height=48, fps=30.0)
    enc.close()
    enc.close()  # should not raise, should not double-close

    assert fake_av.container.close.call_count == 1


def test_write_after_close_raises(tmp_path: Path, fake_av: SimpleNamespace) -> None:
    from syncfield.adapters._video_encoder import VideoEncoder

    enc = VideoEncoder.open(tmp_path / "clip.mp4", width=64, height=48, fps=30.0)
    enc.close()
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    with pytest.raises(RuntimeError):
        enc.write(frame)


def test_close_propagates_flush_error_but_closes_container(
    tmp_path: Path, fake_av: SimpleNamespace
) -> None:
    """If the flush raises, close() still closes the container and re-raises."""
    from syncfield.adapters._video_encoder import VideoEncoder

    enc = VideoEncoder.open(tmp_path / "clip.mp4", width=64, height=48, fps=30.0)

    # Make the flush (encode(None)) raise.
    fake_av.stream.encode = MagicMock(
        side_effect=lambda frame: (
            (_ for _ in ()).throw(RuntimeError("flush failed"))
            if frame is None
            else [fake_av.packet]
        )
    )

    with pytest.raises(RuntimeError, match="flush failed"):
        enc.close()

    # Container was still closed even though flush raised.
    fake_av.container.close.assert_called_once()
    # Second close() is a no-op.
    enc.close()
    fake_av.container.close.assert_called_once()


def test_open_uvc_input_macos(
    monkeypatch: pytest.MonkeyPatch, fake_av: SimpleNamespace
) -> None:
    from syncfield.adapters import _video_encoder

    monkeypatch.setattr(_video_encoder.sys, "platform", "darwin")
    input_container = MagicMock(name="InputContainer")
    fake_av.av.open.return_value = input_container

    result = _video_encoder.open_uvc_input(
        device_index=0, width=1280, height=720, fps=30.0
    )

    args, kwargs = fake_av.av.open.call_args
    assert args[0] == "0"
    assert kwargs.get("format") == "avfoundation"
    assert kwargs.get("options", {}).get("video_size") == "1280x720"
    assert kwargs.get("options", {}).get("framerate") == "30"
    # pixel_format is intentionally omitted by default — forcing mjpeg
    # or any other value breaks macOS built-in cameras that expose
    # yuyv422/nv12 only.
    assert "pixel_format" not in kwargs.get("options", {})
    assert result is input_container


def test_open_uvc_input_linux(
    monkeypatch: pytest.MonkeyPatch, fake_av: SimpleNamespace
) -> None:
    from syncfield.adapters import _video_encoder

    monkeypatch.setattr(_video_encoder.sys, "platform", "linux")
    fake_av.av.open.return_value = MagicMock(name="InputContainer")

    _video_encoder.open_uvc_input(device_index=2, width=1280, height=720, fps=30.0)

    args, kwargs = fake_av.av.open.call_args
    assert args[0] == "/dev/video2"
    assert kwargs.get("format") == "v4l2"


def test_open_uvc_input_windows(
    monkeypatch: pytest.MonkeyPatch, fake_av: SimpleNamespace
) -> None:
    from syncfield.adapters import _video_encoder

    monkeypatch.setattr(_video_encoder.sys, "platform", "win32")
    fake_av.av.open.return_value = MagicMock(name="InputContainer")

    _video_encoder.open_uvc_input(
        device_index=0,
        width=1280,
        height=720,
        fps=30.0,
        device_name="Logitech BRIO",
    )

    args, kwargs = fake_av.av.open.call_args
    assert args[0] == "video=Logitech BRIO"
    assert kwargs.get("format") == "dshow"


def test_open_uvc_input_windows_requires_device_name(
    monkeypatch: pytest.MonkeyPatch, fake_av: SimpleNamespace
) -> None:
    from syncfield.adapters import _video_encoder

    monkeypatch.setattr(_video_encoder.sys, "platform", "win32")

    with pytest.raises(ValueError, match="device_name"):
        _video_encoder.open_uvc_input(device_index=0, width=1280, height=720, fps=30.0)


def test_open_uvc_input_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch, fake_av: SimpleNamespace
) -> None:
    from syncfield.adapters import _video_encoder

    monkeypatch.setattr(_video_encoder.sys, "platform", "freebsd13")

    with pytest.raises(RuntimeError, match="Unsupported platform"):
        _video_encoder.open_uvc_input(device_index=0, width=1280, height=720, fps=30.0)


def test_isolated_remux_runs_the_same_remuxer_in_a_child_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_av: SimpleNamespace,
) -> None:
    from syncfield.adapters import _video_encoder

    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv: list[str], **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_video_encoder.subprocess, "run", fake_run)
    source = tmp_path / "segment.h264"
    destination = tmp_path / "segment.mp4"

    _video_encoder.remux_h264_to_mp4_isolated(
        source, destination, fps=30.0, timeout_s=123
    )

    argv, kwargs = calls[0]
    assert argv[0] == sys.executable
    assert argv[-3:] == [str(source), str(destination), "30.0"]
    assert "remux_h264_to_mp4" in argv[2]
    assert kwargs["timeout"] == 123


def test_isolated_remux_removes_partial_output_on_worker_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_av: SimpleNamespace,
) -> None:
    from syncfield.adapters import _video_encoder

    destination = tmp_path / "partial.mp4"
    destination.write_bytes(b"partial")
    monkeypatch.setattr(
        _video_encoder.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=7, stdout="", stderr="broken container"
        ),
    )

    with pytest.raises(RuntimeError, match="broken container"):
        _video_encoder.remux_h264_to_mp4_isolated(
            tmp_path / "segment.h264", destination, fps=30.0
        )
    assert not destination.exists()


def test_isolated_remux_uses_idle_io_priority_on_linux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_av: SimpleNamespace,
) -> None:
    from syncfield.adapters import _video_encoder

    calls: list[list[str]] = []
    monkeypatch.setattr(_video_encoder.sys, "platform", "linux")
    monkeypatch.setattr(_video_encoder.shutil, "which", lambda name: "/usr/bin/ionice")
    monkeypatch.setattr(
        _video_encoder.subprocess,
        "run",
        lambda argv, **kwargs: (
            calls.append(argv) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    _video_encoder.remux_h264_to_mp4_isolated(
        tmp_path / "segment.h264", tmp_path / "segment.mp4", fps=30.0
    )

    assert calls[0][:3] == ["/usr/bin/ionice", "-c", "3"]
