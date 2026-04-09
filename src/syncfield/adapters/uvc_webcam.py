"""UVCWebcamStream — OpenCV-based reference adapter for UVC/USB webcams.

Requires the optional ``uvc`` extra:

    pip install syncfield[uvc]

The adapter runs a background thread that reads frames in a tight loop,
timestamps each read with ``time.monotonic_ns()`` **before** any further
processing, emits a :class:`~syncfield.types.SampleEvent`, and writes the
frame to an MP4 via ``cv2.VideoWriter``.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Optional

try:
    import cv2  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - exercised via sys.modules patch
    raise ImportError(
        "UVCWebcamStream requires opencv-python. "
        "Install with `pip install syncfield[uvc]`."
    ) from exc

from syncfield.clock import SessionClock
from syncfield.stream import StreamBase
from syncfield.types import (
    FinalizationReport,
    SampleEvent,
    StreamCapabilities,
)


class UVCWebcamStream(StreamBase):
    """Captures video from a UVC webcam via OpenCV.

    Args:
        id: Stream id (also used as the output file name, ``{id}.mp4``).
        device_index: OpenCV device index passed to ``cv2.VideoCapture``.
        output_dir: Directory for the resulting MP4 file.
        width: Desired frame width (or ``None`` to use the device default).
        height: Desired frame height (or ``None`` to use the device default).
        fps: Desired frame rate (or ``None`` to use the device default).
    """

    def __init__(
        self,
        id: str,
        device_index: int,
        output_dir: Path | str,
        width: Optional[int] = None,
        height: Optional[int] = None,
        fps: Optional[float] = None,
    ) -> None:
        super().__init__(
            id=id,
            kind="video",
            capabilities=StreamCapabilities(
                provides_audio_track=False,  # OpenCV webcams have no audio path
                supports_precise_timestamps=True,
                is_removable=True,
                produces_file=True,
            ),
        )
        self._device_index = device_index
        self._output_dir = Path(output_dir)
        self._width = width
        self._height = height
        self._fps = fps

        self._capture: Any = None
        self._writer: Any = None
        self._file_path = self._output_dir / f"{id}.mp4"
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._frame_count = 0
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None

        # Live preview support — the viewer reads ``latest_frame`` to render
        # the stream card thumbnail. ``_frame_lock`` protects handoff between
        # the capture thread and the reader; the frame itself is a plain
        # reference so no copy is made in the hot path.
        self._frame_lock = threading.Lock()
        self._latest_frame: Any = None

    # ------------------------------------------------------------------
    # Stream SPI
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._capture = cv2.VideoCapture(self._device_index)
        if not self._capture.isOpened():
            raise RuntimeError(
                f"cv2.VideoCapture({self._device_index}) failed to open"
            )

    def start(self, session_clock: SessionClock) -> None:
        width, height, fps = self._resolve_frame_geometry()
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._writer = cv2.VideoWriter(
            str(self._file_path), fourcc, fps, (width, height)
        )
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, name=f"uvc-{self.id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> FinalizationReport:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._release_cv2_resources()

        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=self._frame_count,
            file_path=self._file_path if self._frame_count > 0 else None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=None,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_frame_geometry(self) -> tuple[int, int, float]:
        """Pick width, height, fps — constructor overrides beat device defaults."""
        width = self._width or int(
            self._capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 640
        )
        height = self._height or int(
            self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 480
        )
        fps = self._fps or self._capture.get(cv2.CAP_PROP_FPS) or 30.0
        return width, height, fps

    def _capture_loop(self) -> None:
        """Background thread body — read/timestamp/emit/write in a tight loop.

        The timestamp is captured immediately after ``read()`` so the
        jitter between the physical frame and our recorded timestamp
        stays as small as possible.
        """
        assert self._capture is not None
        while not self._stop_event.is_set():
            ok, frame = self._capture.read()
            capture_ns = time.monotonic_ns()
            if not ok or frame is None:
                break
            if self._first_at is None:
                self._first_at = capture_ns
            self._last_at = capture_ns
            self._frame_count += 1

            # Publish the latest frame for live preview (viewer reads this).
            with self._frame_lock:
                self._latest_frame = frame

            if self._writer is not None:
                self._writer.write(frame)
            self._emit_sample(
                SampleEvent(
                    stream_id=self.id,
                    frame_number=self._frame_count - 1,
                    capture_ns=capture_ns,
                )
            )

    # ------------------------------------------------------------------
    # Live preview
    # ------------------------------------------------------------------

    @property
    def latest_frame(self) -> Any:
        """Return the most recently captured BGR frame, or ``None``.

        Thread-safe: the frame reference is published under a lock by the
        capture thread. Readers that mutate the returned array should
        ``.copy()`` it first — in practice the viewer uploads it as a
        texture immediately and never mutates it in place.
        """
        with self._frame_lock:
            return self._latest_frame

    def _release_cv2_resources(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        if self._capture is not None:
            self._capture.release()
            self._capture = None
