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

import sys
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
    ) -> "VideoEncoder":
        """Open ``path`` for writing and configure the H.264 stream."""
        chosen_codec = codec or _pick_h264_encoder()
        container = av.open(str(path), mode="w")
        stream = container.add_stream(chosen_codec, rate=int(round(fps)))
        stream.width = int(width)
        stream.height = int(height)
        stream.pix_fmt = "yuv420p"
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
        """Flush the encoder and close the container. Idempotent.

        May raise if the final flush fails. A second ``close()`` is always
        a no-op. The underlying container is closed even if the flush
        raises, so resources are not leaked.
        """
        if self._closed:
            return
        self._closed = True
        flush_error: Optional[BaseException] = None
        try:
            for packet in self._stream.encode(None):
                self._container.mux(packet)
        except BaseException as exc:  # noqa: BLE001 - propagate after cleanup
            flush_error = exc
        # Always close the container, even if flush raised.
        try:
            self._container.close()
        except Exception:  # noqa: BLE001 - trailer write failure is unrecoverable
            pass
        if flush_error is not None:
            raise flush_error


def open_uvc_input(
    *,
    device_index: int,
    width: int,
    height: int,
    fps: float,
    device_name: Optional[str] = None,
    pixel_format: Optional[str] = None,
) -> "av.container.InputContainer":
    """Open a UVC webcam as a PyAV input container.

    Platform dispatch:

    * macOS — ``avfoundation`` with URL ``"<video>"`` (video index only;
      omitting the ``":<audio>"`` half disables audio capture). The
      ``"<index>:none"`` form is NOT portable across PyAV's bundled
      ffmpeg builds — ``avfoundation_read_header`` tries to look up
      ``"none"`` as an audio device name and fails with EINVAL.
    * Linux — ``v4l2`` with URL ``/dev/video<N>``.
    * Windows — ``dshow`` with URL ``video=<device_name>``. The caller
      must supply ``device_name`` (DirectShow has no index URL).

    Pixel format is left unset by default so avfoundation/v4l2/dshow
    negotiate whatever the camera natively produces. Forcing a value
    like ``"mjpeg"`` fails on cameras that don't expose that format
    (macOS built-in FaceTime cameras expose only ``yuyv422`` /
    ``nv12``). Pass ``pixel_format`` explicitly only when the caller
    knows the device supports it.

    The returned container yields packets via ``.demux()`` which the
    caller decodes frame-by-frame.
    """
    options = {
        "video_size": f"{int(width)}x{int(height)}",
        "framerate": str(int(round(fps))),
    }
    if pixel_format is not None:
        options["pixel_format"] = pixel_format

    if sys.platform == "darwin":
        url = str(int(device_index))
        fmt = "avfoundation"
    elif sys.platform.startswith("linux"):
        url = f"/dev/video{int(device_index)}"
        fmt = "v4l2"
    elif sys.platform.startswith("win"):
        if not device_name:
            raise ValueError(
                "Windows UVC input requires `device_name` "
                "(DirectShow has no device-index URL)."
            )
        url = f"video={device_name}"
        fmt = "dshow"
    else:
        raise RuntimeError(f"Unsupported platform for UVC input: {sys.platform}")

    return av.open(url, format=fmt, options=options)


def compute_jitter_percentiles(
    intervals_ns: list[int],
) -> tuple[Optional[int], Optional[int]]:
    """Return (p95, p99) of inter-frame intervals, or (None, None) if <20 samples.

    At very small sample sizes percentile estimates are noisy and not
    usefully actionable — better to leave them null than to emit a
    misleading number.
    """
    if len(intervals_ns) < 20:
        return None, None
    sorted_iv = sorted(intervals_ns)
    p95_idx = min(len(sorted_iv) - 1, int(len(sorted_iv) * 0.95))
    p99_idx = min(len(sorted_iv) - 1, int(len(sorted_iv) * 0.99))
    return sorted_iv[p95_idx], sorted_iv[p99_idx]


def remux_h264_to_mp4(
    h264_path: str | Path,
    mp4_path: str | Path,
    *,
    fps: float,
) -> None:
    """Remux a raw H.264 Annex-B bitstream into an MP4 container.

    Copy-mode only — no re-encoding. The on-device encoder already
    produced well-formed compressed packets; all this does is wrap
    them in an MP4 ISO-BMFF container so standard players (and
    ffprobe) can decode the file. Runtime is dominated by disk I/O
    and is effectively instant for typical benchmark clips.

    Timestamps: raw Annex-B streams carry no PTS/DTS — the demuxer
    surfaces every packet with ``dts=None``. We synthesise a uniform
    1/fps cadence at a millisecond-granularity timebase so the MP4
    muxer has monotonic timestamps to work with. This matches what
    ``ffmpeg -framerate N -i in.h264 -c copy out.mp4`` does at the
    CLI level.

    Args:
        h264_path: Path to the raw ``.h264`` file (Annex-B with SPS/PPS
            inline; DepthAI's ``VideoEncoder`` emits exactly this).
        mp4_path: Destination MP4 path. Overwritten if it exists.
        fps: Nominal frame rate. Controls both the demuxer's framerate
            hint and the synthesised PTS cadence.
    """
    from fractions import Fraction

    fps_rounded = int(round(fps))
    # Millisecond-granularity timebase — fine enough that sub-frame
    # jitter (captured separately in ``timestamps.jsonl``) is not
    # rounded away, coarse enough that synthesised pts values stay in
    # int64 territory for arbitrarily long sessions.
    time_base = Fraction(1, fps_rounded * 1000)
    frame_duration = 1000  # in ``time_base`` units → 1/fps seconds

    options = {"framerate": str(fps_rounded)}
    input_container = av.open(str(h264_path), format="h264", options=options)
    output_container = av.open(str(mp4_path), mode="w")
    try:
        in_stream = input_container.streams.video[0]
        # PyAV >=14 exposes a dedicated template copy API; the legacy
        # ``add_stream(template=...)`` form that worked through PyAV 13
        # was removed. The ``hasattr`` check keeps us compatible with
        # the ``av>=12`` floor declared in pyproject.toml.
        if hasattr(output_container, "add_stream_from_template"):
            out_stream = output_container.add_stream_from_template(template=in_stream)
        else:
            out_stream = output_container.add_stream(template=in_stream)

        pts = 0
        for packet in input_container.demux(in_stream):
            # PyAV emits a final zero-size "flush" packet at EOF that
            # must not be muxed. Real packets from Annex-B have
            # ``dts=None`` but ``size>0`` — filtering on size avoids
            # the raw/container demuxer asymmetry.
            if packet.size == 0:
                continue
            packet.stream = out_stream
            packet.pts = pts
            packet.dts = pts
            packet.time_base = time_base
            pts += frame_duration
            output_container.mux(packet)
    finally:
        input_container.close()
        output_container.close()
