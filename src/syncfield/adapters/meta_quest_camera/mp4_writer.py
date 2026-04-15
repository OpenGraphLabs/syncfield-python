"""StreamingVideoRecorder — MJPEG-passthrough recorder for Quest streams.

Takes raw JPEG bytes (as produced by Quest's ``/preview/{eye}`` endpoint)
and writes them directly into an MP4 container *without re-encoding*,
plus a sidecar timestamps JSONL with both the host-projected and the
Quest-native nanosecond timestamps for every frame.

Designed to be fed by an :class:`~syncfield.adapters.meta_quest_camera.preview.MjpegPreviewConsumer`'s
frame-sink callback — the recorder doesn't own the network connection.
That keeps the same MJPEG channel serving both the live viewer panel
*and* the recording artifact at no extra Quest-side cost.

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

import json
import logging
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import IO, Optional

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
    timestamps_path: Path
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

        recorder = StreamingVideoRecorder(output_dir=..., ...)
        recorder.start()
        # called repeatedly from the network / sink thread:
        recorder.write_frame(jpeg, host_ns, quest_native_ns)
        result = recorder.stop()

    ``write_frame`` may be called from any thread; an instance lock
    serialises mux / JSONL / state mutations so :meth:`stop` is safe
    to invoke while a sink is still pushing frames in.

    Calls to ``write_frame`` against an unstarted or already-stopped
    recorder are silent no-ops, which simplifies the sink wiring on
    the consumer side (it does not have to mirror the lifecycle).
    """

    def __init__(
        self,
        *,
        output_dir: Path,
        stream_id: str,
        side: str,
        fps: int,
        width: int,
        height: int,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._stream_id = stream_id
        self._side = side
        self._fps = max(1, int(fps))
        self._width = int(width)
        self._height = int(height)

        self._lock = threading.Lock()
        self._container: Optional["av.container.OutputContainer"] = None
        self._stream: Optional["av.video.stream.VideoStream"] = None
        self._timestamps_file: Optional[IO[str]] = None

        self._frame_count = 0
        self._write_errors = 0
        self._first_capture_ns: Optional[int] = None
        self._last_capture_ns: Optional[int] = None
        # PTS anchor — set on the first muxed frame so the file's PTS
        # starts at 0 instead of an enormous absolute monotonic value.
        self._first_pts_us: Optional[int] = None

    @property
    def output_path(self) -> Path:
        return self._output_dir / f"{self._stream_id}_{self._side}.mp4"

    @property
    def timestamps_path(self) -> Path:
        return self._output_dir / f"{self._stream_id}_{self._side}.timestamps.jsonl"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the MP4 container and timestamps JSONL.

        No-op if the recorder is already started. After a :meth:`stop`,
        a fresh ``start()`` reopens the files (overwriting whatever
        was written previously).
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
            except Exception:
                container.close()
                raise

            timestamps_file = open(self.timestamps_path, "w", encoding="utf-8")
            self._container = container
            self._stream = stream
            self._timestamps_file = timestamps_file
            self._frame_count = 0
            self._write_errors = 0
            self._first_capture_ns = None
            self._last_capture_ns = None
            self._first_pts_us = None
            logger.info(
                "[%s/%s] streaming recorder open → %s (%dx%d @ %d fps)",
                self._stream_id, self._side, self.output_path,
                self._width, self._height, self._fps,
            )

    def write_frame(
        self,
        jpeg_bytes: bytes,
        host_ns: int,
        quest_native_ns: Optional[int] = None,
    ) -> None:
        """Mux one JPEG packet and emit one timestamp line."""
        with self._lock:
            container = self._container
            stream = self._stream
            ts_file = self._timestamps_file
            if container is None or stream is None or ts_file is None:
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

                ts_line = json.dumps({
                    "frame_number": self._frame_count,
                    "capture_ns": int(host_ns),
                    "quest_native_ns": (
                        int(quest_native_ns) if quest_native_ns else None
                    ),
                    "clock_domain": "remote_quest3",
                    "uncertainty_ns": 10_000_000,
                })
                ts_file.write(ts_line + "\n")

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
                        "[%s/%s] mux error (frame #%d, total errors=%d): %s",
                        self._stream_id, self._side, self._frame_count,
                        self._write_errors, exc,
                    )

    def stop(self) -> StreamingVideoResult:
        """Flush + close everything and return the artifact paths.

        Idempotent. A second ``stop()`` returns the same result and
        does no further I/O. Errors during close are logged but do not
        propagate — the result still describes whatever we managed to
        write before the failure.
        """
        with self._lock:
            container = self._container
            ts_file = self._timestamps_file
            self._container = None
            self._stream = None
            self._timestamps_file = None

            if container is not None:
                try:
                    container.close()
                except BaseException as exc:  # noqa: BLE001
                    logger.warning(
                        "[%s/%s] container close error: %s",
                        self._stream_id, self._side, exc,
                    )
            if ts_file is not None:
                try:
                    ts_file.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[%s/%s] timestamps close error: %s",
                        self._stream_id, self._side, exc,
                    )

            result = StreamingVideoResult(
                output_path=self.output_path,
                timestamps_path=self.timestamps_path,
                frame_count=self._frame_count,
                first_capture_ns=self._first_capture_ns,
                last_capture_ns=self._last_capture_ns,
                write_errors=self._write_errors,
            )
            logger.info(
                "[%s/%s] streaming recorder closed: %d frames, %d write errors",
                self._stream_id, self._side, result.frame_count, result.write_errors,
            )
            return result
