"""UVCWebcamStream — OpenCV-based reference adapter for UVC/USB webcams.

Requires the optional ``uvc`` extra:

    pip install syncfield[uvc]

The adapter runs a background thread that reads frames in a tight loop,
timestamps each read with ``time.monotonic_ns()`` **before** any further
processing, and publishes them to :attr:`latest_frame` for the viewer
to preview. The **same thread** writes to an MP4 via ``cv2.VideoWriter``
and emits :class:`~syncfield.types.SampleEvent` — but only while the
session is in :attr:`~syncfield.SessionState.RECORDING`. In the
:attr:`~syncfield.SessionState.CONNECTED` live-preview phase the loop
keeps feeding ``latest_frame`` so the viewer card shows a live
thumbnail, without touching the filesystem.

Lifecycle
---------

This adapter implements the 4-phase :class:`~syncfield.Stream` SPI:

* ``prepare()``   — create the output directory and open ``cv2.VideoCapture``.
* ``connect()``   — start the capture thread in preview-only mode;
  ``latest_frame`` begins updating immediately.
* ``start_recording()`` — open the ``VideoWriter`` and flip the
  ``_recording`` flag so the loop starts writing and emitting samples.
* ``stop_recording()`` — flip ``_recording`` off and close the writer;
  the capture thread keeps running so preview continues.
* ``disconnect()`` — stop the capture thread and release the
  ``VideoCapture`` handle.

Legacy ``start()`` / ``stop()`` are still supported for one-shot code
paths (tests, scripts that don't use the viewer): they simply call
``connect() + start_recording()`` and ``stop_recording() + disconnect()``
respectively.
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
from syncfield.stream import DeviceKey, StreamBase
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

    # Class-level hints for ``syncfield.discovery``.
    _discovery_kind = "video"
    _discovery_adapter_type = "uvc_webcam"

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

        # True while the capture loop is writing frames to the MP4
        # writer and emitting ``SampleEvent``s. ``connect()`` leaves
        # this False so the preview phase stays write-free;
        # ``start_recording()`` flips it True; ``stop_recording()``
        # flips it False again while the thread keeps running.
        self._recording = False

        # Live preview support — the viewer reads ``latest_frame`` to render
        # the stream card thumbnail. ``_frame_lock`` protects handoff between
        # the capture thread and the reader; the frame itself is a plain
        # reference so no copy is made in the hot path.
        self._frame_lock = threading.Lock()
        self._latest_frame: Any = None

    @property
    def device_key(self) -> Optional[DeviceKey]:
        """``("uvc_webcam", str(device_index))`` — the OpenCV index is
        the stable hardware id on macOS / Linux / Windows.
        """
        return ("uvc_webcam", str(self._device_index))

    # ------------------------------------------------------------------
    # Stream SPI — 4-phase lifecycle
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        """Open ``cv2.VideoCapture``.

        Called once by the orchestrator before ``connect()``. The
        device handle stays open from here until ``disconnect()``.
        Output directory is created later in ``start_recording()``.
        """
        if self._capture is None:
            self._capture = cv2.VideoCapture(self._device_index)
        if not self._capture.isOpened():
            raise RuntimeError(
                f"cv2.VideoCapture({self._device_index}) failed to open"
            )

    def connect(self) -> None:
        """Start the capture thread in preview-only mode.

        After this call the background thread reads frames as fast as
        the device allows and publishes each one to
        :attr:`latest_frame` so the viewer's stream card shows a live
        thumbnail. **No file is written and no SampleEvents are
        emitted** until ``start_recording()`` flips the
        ``_recording`` flag.

        Idempotent: calling ``connect()`` on an already-connected
        stream is a no-op so the legacy ``start()`` wrapper can call
        it unconditionally without risking a second thread.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        # prepare() is the documented step that opens the capture,
        # but legacy scripts that jump straight to start() route
        # through connect() too — so open the device here if nobody
        # already did.
        if self._capture is None or not self._capture.isOpened():
            self.prepare()
        self._recording = False
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, name=f"uvc-{self.id}", daemon=True
        )
        self._thread.start()

    def start_recording(self, session_clock: SessionClock) -> None:
        """Open the ``VideoWriter`` and flip recording on.

        The capture thread is already running from ``connect()``,
        so this is effectively a flag flip plus one ``cv2.VideoWriter``
        construction. The first frame written after this call lands
        at ``frame_count == 1``.

        If the caller skipped ``connect()`` (legacy ``start()`` path),
        the thread is started first so the writer always has a
        feeder thread to back it.
        """
        if self._thread is None or not self._thread.is_alive():
            self.connect()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        width, height, fps = self._resolve_frame_geometry()
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._writer = cv2.VideoWriter(
            str(self._file_path), fourcc, fps, (width, height)
        )
        self._recording = True

    def stop_recording(self) -> FinalizationReport:
        """Flip recording off, close the writer, emit the report.

        The capture thread **keeps running** after this call so the
        viewer preview stays live and the user can start a new
        recording on the same session without re-opening hardware.
        """
        self._recording = False
        if self._writer is not None:
            self._writer.release()
            self._writer = None
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

    def disconnect(self) -> None:
        """Stop the capture thread and release the device handle.

        Called by the orchestrator when the session returns to
        ``IDLE``. After this call the adapter holds no OS resources.
        Idempotent — calling it twice is safe.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._release_cv2_resources()

    # ------------------------------------------------------------------
    # Legacy one-shot lifecycle
    # ------------------------------------------------------------------

    def start(self, session_clock: SessionClock) -> None:
        """Legacy one-shot start — open writer and start recording immediately.

        Exists so 0.1-era scripts that call ``prepare() → start() →
        stop()`` keep working without changes. The flag and writer
        are set up **before** the capture thread spawns so the very
        first frame off ``read()`` lands in both the file and the
        emitted :class:`SampleEvent` — no preview phase.

        New callers (the viewer, the 4-phase orchestrator path)
        should use :meth:`connect` and :meth:`start_recording`
        directly to get live preview before the first Record click.
        """
        if self._capture is None or not self._capture.isOpened():
            self.prepare()
        width, height, fps = self._resolve_frame_geometry()
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._writer = cv2.VideoWriter(
            str(self._file_path), fourcc, fps, (width, height)
        )
        # Flip the recording flag BEFORE spawning the thread so the
        # loop records from frame 1 instead of racing through a
        # preview phase the caller never asked for.
        self._recording = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, name=f"uvc-{self.id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> FinalizationReport:
        """Legacy one-shot stop — stop_recording + disconnect in one call."""
        report = self.stop_recording()
        self.disconnect()
        return report

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
        """Background thread body — read frames in a tight loop.

        Two phases, distinguished by the ``_recording`` flag:

        * **Preview** (``_recording == False``) — publish every frame
          to ``latest_frame`` so the viewer card stays live, but do
          **not** write to the file, update frame counters, or emit
          ``SampleEvent``. The capture runs continuously from the
          moment ``connect()`` spawned this thread.
        * **Recording** (``_recording == True``) — same preview
          publish, plus append the frame to the ``VideoWriter``,
          advance ``_frame_count``, and emit ``SampleEvent``. The
          capture timestamp is sampled immediately after ``read()``
          so the jitter between the physical frame and the recorded
          monotonic time stays as small as possible.

        The loop exits when ``_stop_event`` fires (from
        ``disconnect()``) or when ``read()`` returns a falsy result
        (hardware disconnect / device busy).
        """
        assert self._capture is not None
        while not self._stop_event.is_set():
            ok, frame = self._capture.read()
            capture_ns = time.monotonic_ns()
            if not ok or frame is None:
                break

            # Always publish for live preview — the viewer reads this
            # in both CONNECTED and RECORDING states.
            with self._frame_lock:
                self._latest_frame = frame

            if self._recording:
                if self._first_at is None:
                    self._first_at = capture_ns
                self._last_at = capture_ns
                self._frame_count += 1
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

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> list:
        """Enumerate attached UVC webcams.

        Uses the platform-native enumeration tool when available so the
        discovery pass is fast and avoids triggering the camera permission
        dialog (which ``cv2.VideoCapture`` does on macOS even for a probe):

        - macOS: ``system_profiler SPCameraDataType -json``
        - Linux: ``/dev/video*`` inspection
        - Other / fallback: nothing returned — use explicit
          ``UVCWebcamStream(device_index=...)`` construction

        Each returned device's ``construct_kwargs`` carries the OpenCV
        ``device_index`` that matches its position in the native listing.
        On some platforms that mapping is not perfectly stable — if the
        resulting stream fails to open, users fall back to explicit
        construction.

        Returns:
            List of :class:`~syncfield.discovery.DiscoveredDevice`. Empty
            on unsupported platforms or if the probe raises.
        """
        from syncfield.discovery import DiscoveredDevice

        import sys

        if sys.platform == "darwin":
            raw = _discover_uvc_macos()
        elif sys.platform.startswith("linux"):
            raw = _discover_uvc_linux()
        else:
            raw = []

        return [
            DiscoveredDevice(
                adapter_type="uvc_webcam",
                adapter_cls=cls,
                kind="video",
                display_name=entry["name"],
                description=entry.get("description", "uvc"),
                device_id=str(entry["index"]),
                construct_kwargs={"device_index": int(entry["index"])},
                accepts_output_dir=True,
            )
            for entry in raw
        ]


# ---------------------------------------------------------------------------
# Platform-specific UVC enumeration — factored out so each platform can be
# unit-tested in isolation with a subprocess/filesystem mock.
# ---------------------------------------------------------------------------


def _discover_uvc_macos() -> list[dict]:
    """macOS enumeration via ``system_profiler SPCameraDataType``.

    The tool is free to execute, doesn't prompt for camera permissions,
    and gives us the human-readable name that shows up in System Settings.
    We map its array position to the OpenCV device_index — on almost all
    MacBooks this mapping is stable (built-in at 0, Continuity Camera at
    1, external at 2, ...).
    """
    import json
    import subprocess

    try:
        result = subprocess.run(
            ["system_profiler", "SPCameraDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    cameras = data.get("SPCameraDataType", [])
    entries: list[dict] = []
    for index, item in enumerate(cameras):
        name = item.get("_name") or f"Camera {index}"
        model = item.get("spcamera_model-id", "")
        description = "uvc" if not model else f"uvc · {model}"
        entries.append(
            {"index": index, "name": name, "description": description}
        )
    return entries


def _discover_uvc_linux() -> list[dict]:
    """Linux enumeration via ``/dev/video*`` + optional name lookup.

    Reads ``/sys/class/video4linux/videoN/name`` for each ``/dev/videoN``
    — that's what ``v4l2-ctl --list-devices`` uses internally. No
    subprocess needed.
    """
    import re
    from pathlib import Path as _Path

    entries: list[dict] = []
    video_dir = _Path("/dev")
    if not video_dir.exists():
        return []

    device_files = sorted(
        video_dir.glob("video*"),
        key=lambda p: int(re.sub(r"\D", "", p.name) or "0"),
    )
    for device_file in device_files:
        match = re.match(r"video(\d+)$", device_file.name)
        if not match:
            continue
        index = int(match.group(1))

        # Try to read the human-readable name via sysfs. Falls back to
        # the device path when sysfs isn't available.
        sysfs_name = _Path(f"/sys/class/video4linux/{device_file.name}/name")
        if sysfs_name.exists():
            try:
                name = sysfs_name.read_text().strip() or device_file.name
            except OSError:
                name = device_file.name
        else:
            name = device_file.name

        entries.append(
            {
                "index": index,
                "name": name,
                "description": f"uvc · {device_file}",
            }
        )
    return entries
