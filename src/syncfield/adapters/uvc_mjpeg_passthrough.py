"""MJPEGPassthroughStream — record a UVC camera's own MJPEG, no re-encode.

Why this exists (measured on a Raspberry Pi 5, two 1080p30 cameras,
2026-07-22): software H.264 (libx264) tops out at ~26 fps for a SINGLE
1080p30 stream even at ultrafast/zerolatency — two concurrent streams get
~16 fps each and the machine is saturated. The camera already compresses
every frame to JPEG on its own silicon; storing exactly those bytes needs
only demux+mux (~0 CPU, 30.0 fps measured, ~2.8 MB/s per camera at 1080p30).

Trade-offs, made deliberately:

* Files are ~5-10x larger than H.264. Review playback uses the pipeline's
  cloud-transcoded ``.synced.mp4`` outputs, so raw MJPEG-in-MP4 never needs
  to play in a browser.
* ``latest_frame`` (the preview tap) updates at ``preview_decode_interval_s``
  (default 0.5 s -> ~2 fps), not per frame: decoding 1080p MJPEG at 30 fps
  just for a preview is exactly the kind of 24/7 CPU burn a fanless kiosk
  cannot afford. Every JPEG frame is a keyframe, so sparse decode is safe.

Timestamps: same contract as every video adapter — ``SampleEvent.capture_ns``
per recorded frame (host monotonic, stamped at demux) feeds the
orchestrator's ``{id}.timestamps.jsonl``. Container pts additionally follow
the capture timestamps (90 kHz timebase), so playback pacing reflects real
capture pacing instead of pretending the camera was metronome-perfect.
"""

from __future__ import annotations

import logging
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional

try:
    import av  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - same guard as _video_encoder
    raise ImportError(
        "syncfield video adapters require PyAV. "
        "Install with `pip install syncfield[uvc]`."
    ) from exc

from syncfield.adapters.uvc_webcam import UVCWebcamStream
from syncfield.types import HealthEvent, HealthEventKind, SampleEvent

logger = logging.getLogger(__name__)

#: Container timebase for passthrough output. 90 kHz is the MPEG convention
#: and gives ~11 us pts resolution — far finer than any camera jitter.
_PTS_TIMEBASE = Fraction(1, 90_000)


class PassthroughWriter:
    """Mux already-compressed packets into an MP4, pts from capture time.

    Mirrors ``VideoEncoder``'s narrow interface shape (open/close) so
    ``UVCWebcamStream.stop_recording`` can close it via the same attribute.
    """

    def __init__(self, container: Any, stream: Any) -> None:
        self._container = container
        self._stream = stream
        self._first_capture_ns: Optional[int] = None
        self._last_pts: int = -1
        self._closed = False

    @classmethod
    def open(cls, path: str | Path, *, template_stream: Any) -> "PassthroughWriter":
        container = av.open(str(path), mode="w")
        stream = container.add_stream_from_template(template_stream)
        stream.time_base = _PTS_TIMEBASE
        return cls(container, stream)

    def write_packet(self, packet: Any, capture_ns: int) -> None:
        """Mux one compressed frame, stamped at its host capture time."""
        if self._closed:
            raise RuntimeError("PassthroughWriter.write_packet called after close")
        if self._first_capture_ns is None:
            self._first_capture_ns = capture_ns
        pts = int((capture_ns - self._first_capture_ns) * 90_000 // 1_000_000_000)
        # MP4 requires strictly increasing dts; two frames landing inside one
        # 90 kHz tick (never in practice) must not violate that.
        if pts <= self._last_pts:
            pts = self._last_pts + 1
        self._last_pts = pts
        packet.stream = self._stream
        packet.time_base = _PTS_TIMEBASE
        packet.pts = pts
        packet.dts = pts
        self._container.mux(packet)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._container.close()


class MJPEGPassthroughStream(UVCWebcamStream):
    """UVC capture that records the camera's MJPEG bytes verbatim.

    Same 4-phase Stream SPI, file contract (``{id}.mp4``), SampleEvents,
    FinalizationReport, and ``latest_frame`` tap as ``UVCWebcamStream`` —
    only the capture loop differs (demux+mux instead of decode+encode).
    Linux/pyav only: this is the kiosk recorder, not the desktop app.
    """

    def __init__(
        self,
        id: str,
        device_index: int,
        output_dir: str | Path,
        *,
        width: int = 1920,
        height: int = 1080,
        fps: float = 30.0,
        output_name: Optional[str] = None,
        calibration: Any = None,
        preview_decode_interval_s: float = 0.5,
    ) -> None:
        super().__init__(
            id,
            device_index=device_index,
            output_dir=output_dir,
            width=width,
            height=height,
            fps=fps,
            backend="pyav",
            pixel_format="mjpeg",
            output_name=output_name,
            calibration=calibration,
        )
        self._preview_decode_interval_s = float(preview_decode_interval_s)
        self._last_preview_decode_monotonic = 0.0
        self._preview_decoder: Any = None

    # -- recording lifecycle ------------------------------------------------

    def start_recording(self, session_clock: Any) -> None:
        """Open the passthrough muxer and flip recording on.

        Mirrors the parent's body except the writer: the parent's
        ``stop_recording`` closes ``self._encoder`` duck-typed, so the
        muxer slots straight in.
        """
        self._begin_recording_window(session_clock)
        if self._thread is None or not self._thread.is_alive():
            self.connect()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._write_calibration_file()
        self._frame_count = 0
        self._first_at = None
        self._last_at = None
        self._prev_capture_ns = None
        self._intervals_ns = []
        self._encoder = PassthroughWriter.open(
            self._file_path, template_stream=self._input.streams.video[0]
        )
        self._recording = True

    # -- capture loop -------------------------------------------------------

    def _capture_loop(self) -> None:  # overrides backend dispatch entirely
        self._capture_loop_passthrough()

    def _capture_loop_passthrough(self) -> None:
        """Demux packets; mux while recording; sparse-decode for preview.

        Transient-error semantics mirror ``_capture_loop_pyav``: EAGAIN /
        EINTR during warmup are "try again", real errors surface as a health
        ERROR and end the thread. The demux generator dies on any exception,
        so it is recreated after a transient one.
        """
        _TRANSIENT_ERRNOS = {4, 11, 35}
        packet_iter = None
        while not self._stop_event.is_set():
            if packet_iter is None:
                packet_iter = self._input.demux(video=0)
            try:
                packet = next(packet_iter)
                if packet.size == 0:
                    continue
                capture_ns = time.monotonic_ns()

                if self._recording:
                    self._observe_first_frame(capture_ns, None)
                    if self._prev_capture_ns is not None:
                        self._intervals_ns.append(capture_ns - self._prev_capture_ns)
                    self._prev_capture_ns = capture_ns

                    if self._first_at is None:
                        self._first_at = capture_ns
                    self._last_at = capture_ns
                    self._frame_count += 1
                    writer = self._encoder
                    if writer is not None:
                        writer.write_packet(packet, capture_ns)
                    self._emit_sample(
                        SampleEvent(
                            stream_id=self.id,
                            frame_number=self._frame_count - 1,
                            capture_ns=capture_ns,
                        )
                    )

                self._maybe_decode_preview(packet)
            except StopIteration:
                break
            except OSError as exc:
                if exc.errno in _TRANSIENT_ERRNOS or exc.errno is None:
                    packet_iter = None
                    time.sleep(0.001)
                    continue
                self._emit_health(
                    HealthEvent(
                        stream_id=self.id,
                        kind=HealthEventKind.ERROR,
                        at_ns=time.monotonic_ns(),
                        detail=f"passthrough capture loop ended: {exc!r}",
                    )
                )
                return
            except Exception as exc:  # noqa: BLE001 - PyAV surfaces diverse errors
                self._emit_health(
                    HealthEvent(
                        stream_id=self.id,
                        kind=HealthEventKind.ERROR,
                        at_ns=time.monotonic_ns(),
                        detail=f"passthrough capture loop ended: {exc!r}",
                    )
                )
                return

    def _maybe_decode_preview(self, packet: Any) -> None:
        """Decode at most one frame per ``preview_decode_interval_s``.

        Every MJPEG frame is a keyframe, so any packet decodes standalone.
        Never raises — a corrupt frame just skips one preview update.
        """
        now = time.monotonic()
        if now - self._last_preview_decode_monotonic < self._preview_decode_interval_s:
            return
        self._last_preview_decode_monotonic = now
        try:
            if self._preview_decoder is None:
                self._preview_decoder = av.CodecContext.create("mjpeg", "r")
            frames = self._preview_decoder.decode(packet)
            if frames:
                frame_bgr = frames[-1].to_ndarray(format="bgr24")
                with self._frame_lock:
                    self._latest_frame = frame_bgr
        except Exception:  # noqa: BLE001 - preview must never hurt capture
            logger.debug("preview decode failed for %s", self.id, exc_info=True)
