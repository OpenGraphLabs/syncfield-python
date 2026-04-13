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
    stream.encode.return_value = [packet]

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
    # the fake ``av`` when re-imported.
    sys.modules.pop("syncfield.adapters._video_encoder", None)
    monkeypatch.setitem(sys.modules, "av", av)
    return SimpleNamespace(av=av, container=container, stream=stream, packet=packet)


def test_open_creates_output_container(tmp_path: Path, fake_av: SimpleNamespace) -> None:
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


def test_write_encodes_and_muxes_one_frame(tmp_path: Path, fake_av: SimpleNamespace) -> None:
    from syncfield.adapters._video_encoder import VideoEncoder

    enc = VideoEncoder.open(tmp_path / "clip.mp4", width=64, height=48, fps=30.0)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)

    enc.write(frame)

    fake_av.av.VideoFrame.from_ndarray.assert_called_once()
    fake_av.stream.encode.assert_called()
    fake_av.container.mux.assert_called_with(fake_av.packet)

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
