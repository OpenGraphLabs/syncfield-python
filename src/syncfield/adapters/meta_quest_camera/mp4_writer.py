"""StreamingVideoRecorder — MJPEG-passthrough recorder for Quest streams.

Takes raw JPEG bytes (as produced by Quest's ``/preview/{eye}`` endpoint)
and writes them directly into an MP4 container *without re-encoding*.

The orchestrator's auto-jsonl writer captures every per-frame
:class:`~syncfield.types.SampleEvent` we emit, so this recorder
deliberately does NOT write its own ``*.timestamps.jsonl`` — that
sidecar would either collide with the orchestrator's authoritative
record or duplicate its content. ``quest_native_ns`` (needed for
post-hoc clock-drift correction) rides through the SampleEvent's
``channels`` dict instead.

Designed to be fed by an :class:`~syncfield.adapters.meta_quest_camera.preview.MjpegPreviewConsumer`'s
frame-sink callback — the recorder doesn't own the network connection.
That keeps the same MJPEG channel serving both the live viewer panel
and the recording artifact at no extra Quest-side cost.

Why MJPEG passthrough rather than re-encode to H.264:

* **Zero CPU on the Mac.** No JPEG decode + H.264 encode round trip.
* **Bit-exact preservation of quality** — recorded frames are
  byte-identical to what the Quest sender encoded, so quality is
  controlled in one place (``previewJpegQuality`` on the device).
* **Variable framerate is honest.** PTS is derived from the real
  capture timestamps, so jittered WiFi delivery shows up in the file
  rather than being smoothed away to look like a flawless 30 fps.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Optional

try:
    import av  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - exercised via sys.modules patch
    raise ImportError(
        "MetaQuestCameraStream requires PyAV. "
        "Install with `pip install syncfield[viewer]` (or [oak], [uvc])."
    ) from exc


logger = logging.getLogger(__name__)


# Microsecond time-base for the muxed MP4. Fine enough to represent
# Quest's 30 Hz captures without two adjacent PTS colliding (≥33 333 µs
# apart at 30 fps), and below the WiFi-jitter floor we're aiming for.
_MP4_TIME_BASE_DEN = 1_000_000


@dataclass
class StreamingVideoResult:
    """Returned by :meth:`StreamingVideoRecorder.stop`."""

    output_path: Path
    frame_count: int
    first_capture_ns: Optional[int]
    last_capture_ns: Optional[int]
    # Frames the writer accepted but failed to mux. Surfaces silent
    # disk / container errors that would otherwise vanish (each
    # individual write swallows the exception so the recorder can
    # keep going).
    write_errors: int


class StreamingVideoRecorder:
    """Writes a stream of JPEG frames into an MJPEG-in-MP4 container.

    Lifecycle::

        recorder = StreamingVideoRecorder(output_dir=..., stream_id=..., ...)
        recorder.start()
        # called repeatedly from the network / sink thread:
        recorder.write_frame(jpeg, host_ns, quest_native_ns)
        result = recorder.stop()

    ``write_frame`` may be called from any thread; an instance lock
    serialises mux / state mutations so :meth:`stop` is safe to invoke
    while a sink is still pushing frames in.

    Calls to ``write_frame`` against an unstarted or already-stopped
    recorder are silent no-ops, which simplifies the sink wiring on
    the consumer side (it does not have to mirror the lifecycle).
    """

    def __init__(
        self,
        *,
        output_dir: Path,
        stream_id: str,
        fps: int,
        width: int,
        height: int,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._stream_id = stream_id
        self._fps = max(1, int(fps))
        self._width = int(width)
        self._height = int(height)

        self._lock = threading.Lock()
        self._container: Optional["av.container.OutputContainer"] = None
        self._stream: Optional["av.video.stream.VideoStream"] = None

        self._frame_count = 0
        self._write_errors = 0
        self._first_capture_ns: Optional[int] = None
        self._last_capture_ns: Optional[int] = None
        # PTS anchor — set on the first muxed frame so the file's PTS
        # starts at 0 instead of an enormous absolute monotonic value.
        self._first_pts_us: Optional[int] = None

    @property
    def output_path(self) -> Path:
        return self._output_dir / f"{self._stream_id}.mp4"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the MP4 container.

        No-op if the recorder is already started. After a :meth:`stop`,
        a fresh ``start()`` reopens the file (overwriting whatever was
        written previously).
        """
        with self._lock:
            if self._container is not None:
                return
            self._output_dir.mkdir(parents=True, exist_ok=True)

            container = av.open(str(self.output_path), mode="w")
            try:
                stream = container.add_stream("mjpeg", rate=self._fps)
                stream.width = self._width
                stream.height = self._height
                # JPEG defaults to yuvj420p (full-range chroma); declaring
                # this matches the muxer's expectation for MJPEG packets
                # produced by Quest's Unity encoder.
                stream.pix_fmt = "yuvj420p"
                # Microsecond time base lets PTS reflect real capture
                # times — variable inter-frame intervals from WiFi
                # jitter are preserved instead of being snapped to a
                # synthetic 30 fps grid.
                stream.time_base = Fraction(1, _MP4_TIME_BASE_DEN)
                # Pin the average frame rate via the codec context —
                # without this, the MP4 muxer derives r_frame_rate
                # from the time_base denominator and reports e.g.
                # 1000000/1 instead of 30/1, which downstream tooling
                # (sync engine FPS estimator) then reads as a 1 MHz
                # video and computes nonsense drift values.
                stream.codec_context.framerate = Fraction(self._fps, 1)
            except Exception:
                container.close()
                raise

            self._container = container
            self._stream = stream
            self._frame_count = 0
            self._write_errors = 0
            self._first_capture_ns = None
            self._last_capture_ns = None
            self._first_pts_us = None
            logger.info(
                "[%s] streaming recorder open → %s (%dx%d @ %d fps)",
                self._stream_id, self.output_path,
                self._width, self._height, self._fps,
            )

    def write_frame(
        self,
        jpeg_bytes: bytes,
        host_ns: int,
        quest_native_ns: Optional[int] = None,
    ) -> None:
        """Mux one JPEG packet into the MP4."""
        # quest_native_ns is accepted (and intentionally unused here)
        # for symmetry with the sink callback signature; the value is
        # persisted via the SampleEvent's channels dict in the
        # orchestrator's jsonl rather than a recorder-side sidecar.
        del quest_native_ns

        with self._lock:
            container = self._container
            stream = self._stream
            if container is None or stream is None:
                # Not started or already stopped — silently drop.
                return

            try:
                if self._first_pts_us is None:
                    self._first_pts_us = host_ns // 1000
                pts_us = (host_ns // 1000) - self._first_pts_us

                packet = av.Packet(jpeg_bytes)
                packet.stream = stream
                packet.pts = pts_us
                packet.dts = pts_us
                # Nominal duration so MP4 readers that don't compute
                # from PTS-deltas (some players) still get sensible
                # per-frame timing.
                packet.duration = max(1, _MP4_TIME_BASE_DEN // self._fps)
                container.mux(packet)

                if self._first_capture_ns is None:
                    self._first_capture_ns = int(host_ns)
                self._last_capture_ns = int(host_ns)
                self._frame_count += 1
            except Exception as exc:  # noqa: BLE001 — keep recorder alive
                self._write_errors += 1
                # Log first failure loudly, then once every 30 to avoid
                # drowning logcat if the container goes permanently bad.
                if self._write_errors == 1 or self._write_errors % 30 == 0:
                    logger.warning(
                        "[%s] mux error (frame #%d, total errors=%d): %s",
                        self._stream_id, self._frame_count,
                        self._write_errors, exc,
                    )

    def stop(self) -> StreamingVideoResult:
        """Flush + close the container and return the artifact path.

        Idempotent. A second ``stop()`` returns the same result and
        does no further I/O. Errors during close are logged but do not
        propagate — the result still describes whatever we managed to
        write before the failure.
        """
        with self._lock:
            container = self._container
            self._container = None
            self._stream = None

            if container is not None:
                try:
                    container.close()
                except BaseException as exc:  # noqa: BLE001
                    logger.warning(
                        "[%s] container close error: %s",
                        self._stream_id, exc,
                    )

            result = StreamingVideoResult(
                output_path=self.output_path,
                frame_count=self._frame_count,
                first_capture_ns=self._first_capture_ns,
                last_capture_ns=self._last_capture_ns,
                write_errors=self._write_errors,
            )
            logger.info(
                "[%s] streaming recorder closed: %d frames, %d write errors",
                self._stream_id, result.frame_count, result.write_errors,
            )
            return result
