"""VideoEncoder — shared PyAV-based MP4 writer for video adapters.

Used by :class:`~syncfield.adapters.uvc_webcam.UVCWebcamStream` and
:class:`~syncfield.adapters.oak_camera.OakCameraStream`. The interface is
deliberately narrow: open with geometry, write BGR numpy frames, close.

The encoder auto-selects the best available H.264 encoder:

* ``h264_videotoolbox`` on macOS (hardware, near-zero CPU)
* ``libx264`` everywhere else (software, widely available)

All frames are assumed to be BGR24 (numpy ``uint8``, shape
``(height, width, 3)``) to match the rest of the SDK's convention.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

try:
    import av  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - exercised via sys.modules patch
    raise ImportError(
        "syncfield video adapters require PyAV. "
        "Install with `pip install syncfield[uvc]` (or [oak], [viewer])."
    ) from exc


def _pick_h264_encoder() -> str:
    """Return the best H.264 encoder name available in this FFmpeg build."""
    for candidate in ("h264_videotoolbox", "libx264"):
        try:
            av.codec.Codec(candidate, "w")
        except Exception:  # noqa: BLE001 - PyAV raises generic errors here
            continue
        return candidate
    raise RuntimeError(
        "No H.264 encoder found in PyAV. Reinstall `av` with libx264 support."
    )


class VideoEncoder:
    """Thin wrapper around an ``av`` output container + H.264 stream."""

    def __init__(
        self,
        container: "av.container.OutputContainer",
        stream: "av.video.stream.VideoStream",
    ) -> None:
        self._container = container
        self._stream = stream
        self._closed = False

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        width: int,
        height: int,
        fps: float,
        codec: Optional[str] = None,
        pixel_format: str = "yuv420p",
    ) -> "VideoEncoder":
        """Open ``path`` for writing and configure the H.264 stream."""
        chosen_codec = codec or _pick_h264_encoder()
        container = av.open(str(path), mode="w")
        stream = container.add_stream(chosen_codec, rate=int(round(fps)))
        stream.width = int(width)
        stream.height = int(height)
        stream.pix_fmt = pixel_format
        return cls(container, stream)

    def write(self, frame_bgr: np.ndarray) -> None:
        """Encode and mux a single BGR frame.

        Must not be called after :meth:`close`. Callers that interleave
        writes with other hot-path work should keep the frame buffer
        alive until this call returns.
        """
        if self._closed:
            raise RuntimeError("VideoEncoder.write called after close")
        video_frame = av.VideoFrame.from_ndarray(frame_bgr, format="bgr24")
        for packet in self._stream.encode(video_frame):
            self._container.mux(packet)

    def close(self) -> None:
        """Flush the encoder and close the container. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            for packet in self._stream.encode(None):
                self._container.mux(packet)
        finally:
            self._container.close()
