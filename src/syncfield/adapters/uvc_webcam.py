"""UVCWebcamStream — PyAV-based adapter for UVC/USB webcams.

Requires the optional ``uvc`` extra:

    pip install syncfield[uvc]

The adapter runs a background thread that decodes frames from a PyAV
input container, timestamps each decoded frame with
``time.monotonic_ns()`` **before** any further processing, and publishes
them to :attr:`latest_frame` for the viewer to preview. The **same
thread** writes to an MP4 via :class:`~._video_encoder.VideoEncoder` and
emits :class:`~syncfield.types.SampleEvent` — but only while the session
is in :attr:`~syncfield.SessionState.RECORDING`.

Lifecycle
---------

This adapter implements the 4-phase :class:`~syncfield.Stream` SPI:

* ``prepare()``   — open the PyAV input container.
* ``connect()``   — start the capture thread in preview-only mode;
  ``latest_frame`` begins updating immediately.
* ``start_recording()`` — open the :class:`VideoEncoder` and flip the
  ``_recording`` flag so the loop starts writing and emitting samples.
* ``stop_recording()`` — flip ``_recording`` off and close the encoder;
  the capture thread keeps running so preview continues.
* ``disconnect()`` — stop the capture thread and release the input
  container.

Legacy ``start()`` / ``stop()`` are still supported for one-shot code
paths (tests, scripts that don't use the viewer).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Optional

from syncfield.adapters._video_encoder import (
    VideoEncoder,
    compute_jitter_percentiles,
    open_uvc_input,
)
from syncfield.clock import SessionClock
from syncfield.stream import DeviceKey, StreamBase
from syncfield.types import (
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    StreamCapabilities,
)


class UVCWebcamStream(StreamBase):
    """Captures video from a UVC webcam via PyAV.

    Args:
        id: Stream id (also used as the output file name, ``{id}.mp4``).
        device_index: Platform device index (AVFoundation / V4L2 index,
            or DirectShow fallback alongside ``device_name``).
        output_dir: Directory for the resulting MP4 file.
        width: Desired frame width (defaults to 1280 — 720p).
        height: Desired frame height (defaults to 720 — 720p).
        fps: Desired frame rate (defaults to 30.0).
        device_name: Required only on Windows (DirectShow). Ignored on
            macOS / Linux — see :func:`~syncfield.adapters._video_encoder.open_uvc_input`.
    """

    # Class-level hints for ``syncfield.discovery``.
    _discovery_kind = "video"
    _discovery_adapter_type = "uvc_webcam"

    def __init__(
        self,
        id: str,
        device_index: int,
        output_dir: Path | str,
        width: Optional[int] = 1280,
        height: Optional[int] = 720,
        fps: Optional[float] = 30.0,
        device_name: Optional[str] = None,
    ) -> None:
        super().__init__(
            id=id,
            kind="video",
            capabilities=StreamCapabilities(
                provides_audio_track=False,
                supports_precise_timestamps=True,
                is_removable=True,
                produces_file=True,
            ),
        )
        self._device_index = device_index
        self._device_name = device_name
        self._output_dir = Path(output_dir)
        self._width = int(width) if width else 1280
        self._height = int(height) if height else 720
        self._fps = float(fps) if fps else 30.0

        self._input: Any = None
        self._encoder: Optional[VideoEncoder] = None
        self._file_path = self._output_dir / f"{id}.mp4"

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._frame_count = 0
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None

        # Jitter collection: inter-frame intervals (ns) over the current
        # recording window. Reset in start_recording() / start().
        self._prev_capture_ns: Optional[int] = None
        self._intervals_ns: list[int] = []

        # True while the capture loop is writing frames and emitting
        # SampleEvents. ``connect()`` leaves this False so the preview
        # phase stays write-free; ``start_recording()`` flips it True.
        self._recording = False

        # Live preview support — the viewer reads ``latest_frame`` for
        # the stream card thumbnail. ``_frame_lock`` protects the
        # reference handoff between the capture thread and readers.
        self._frame_lock = threading.Lock()
        self._latest_frame: Any = None

    @property
    def device_key(self) -> Optional[DeviceKey]:
        """``("uvc_webcam", str(device_index))`` — stable hardware id."""
        return ("uvc_webcam", str(self._device_index))

    # ------------------------------------------------------------------
    # Stream SPI — 4-phase lifecycle
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        """Open the PyAV input container (the camera device)."""
        if self._input is None:
            self._input = open_uvc_input(
                device_index=self._device_index,
                width=self._width,
                height=self._height,
                fps=self._fps,
                device_name=self._device_name,
            )

    def connect(self) -> None:
        """Start the capture thread in preview-only mode."""
        if self._thread is not None and self._thread.is_alive():
            return
        if self._input is None:
            self.prepare()
        self._recording = False
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, name=f"uvc-{self.id}", daemon=True
        )
        self._thread.start()

    def start_recording(self, session_clock: SessionClock) -> None:
        """Open the VideoEncoder and flip recording on."""
        if self._thread is None or not self._thread.is_alive():
            self.connect()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        # Reset per-recording counters so a second recording in the
        # same session starts clean.
        self._frame_count = 0
        self._first_at = None
        self._last_at = None
        self._prev_capture_ns = None
        self._intervals_ns = []
        self._encoder = VideoEncoder.open(
            self._file_path,
            width=self._width,
            height=self._height,
            fps=self._fps,
        )
        self._recording = True

    def stop_recording(self) -> FinalizationReport:
        """Flip recording off, close the encoder, emit the report."""
        self._recording = False
        if self._encoder is not None:
            self._encoder.close()
            self._encoder = None
        jitter_p95, jitter_p99 = compute_jitter_percentiles(self._intervals_ns)
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=self._frame_count,
            file_path=self._file_path if self._frame_count > 0 else None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=None,
            jitter_p95_ns=jitter_p95,
            jitter_p99_ns=jitter_p99,
        )

    def disconnect(self) -> None:
        """Stop the capture thread and release the input container."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._release_av_resources()

    # ------------------------------------------------------------------
    # Legacy one-shot lifecycle
    # ------------------------------------------------------------------

    def start(self, session_clock: SessionClock) -> None:
        """Legacy one-shot start — open encoder and start recording."""
        if self._input is None:
            self.prepare()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._frame_count = 0
        self._first_at = None
        self._last_at = None
        self._prev_capture_ns = None
        self._intervals_ns = []
        self._encoder = VideoEncoder.open(
            self._file_path,
            width=self._width,
            height=self._height,
            fps=self._fps,
        )
        self._recording = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, name=f"uvc-{self.id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> FinalizationReport:
        """Legacy one-shot stop — stop_recording + disconnect."""
        report = self.stop_recording()
        self.disconnect()
        return report

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        """Background thread — decode frames in a tight loop.

        Two phases, distinguished by ``_recording`` flag:

        * Preview — publish to ``latest_frame`` only.
        * Recording — also encode to MP4 and emit SampleEvent.

        The loop exits when ``_stop_event`` fires or the input container
        is exhausted (device disconnect).

        Transient OS errors (EAGAIN / EINTR) raised by AVFoundation or
        V4L2 during camera warmup are NOT fatal — the demuxer is just
        saying "no frame ready yet, try again." We sleep briefly and
        keep polling. Only genuinely fatal errors surface as a health
        event and terminate the thread.
        """
        assert self._input is not None
        # Errno values treated as transient "retry soon":
        # macOS EAGAIN=35, Linux EAGAIN=11, EINTR=4. ``None`` is also
        # treated as transient because PyAV sometimes omits errno on
        # "not ready" conditions.
        _TRANSIENT_ERRNOS = {4, 11, 35}

        # PyAV's container.decode() is a generator that DIES when any
        # exception (including BlockingIOError/EAGAIN) propagates out of
        # it — subsequent next() calls raise StopIteration against a
        # dead iterator. For live camera inputs where EAGAIN is routine
        # during warmup and between frames, we therefore track the
        # iterator explicitly and recreate it after every EAGAIN so the
        # capture thread keeps pulling fresh packets from the container.
        frame_iter = None
        while not self._stop_event.is_set():
            if frame_iter is None:
                frame_iter = iter(self._input.decode(video=0))
            try:
                frame = next(frame_iter)
                capture_ns = time.monotonic_ns()
                if self._recording:
                    if self._prev_capture_ns is not None:
                        self._intervals_ns.append(capture_ns - self._prev_capture_ns)
                    self._prev_capture_ns = capture_ns

                frame_bgr = frame.to_ndarray(format="bgr24")

                with self._frame_lock:
                    self._latest_frame = frame_bgr

                if self._recording:
                    if self._first_at is None:
                        self._first_at = capture_ns
                    self._last_at = capture_ns
                    self._frame_count += 1
                    if self._encoder is not None:
                        self._encoder.write(frame_bgr)
                    self._emit_sample(
                        SampleEvent(
                            stream_id=self.id,
                            frame_number=self._frame_count - 1,
                            capture_ns=capture_ns,
                        )
                    )
            except StopIteration:
                # The decode generator ended cleanly. For a live capture
                # device this means the container was closed / the device
                # was disconnected. Exit the loop.
                break
            except OSError as exc:
                # AVFoundation / V4L2 can surface "not ready" as:
                # - av.error.BlockingIOError (subclass of OSError)
                # - bare OSError(35) on macOS, OSError(11) on Linux
                # - FFmpegError with errno=None
                # All are transient and must not kill the capture loop.
                # The generator is dead after any exception — recreate.
                if exc.errno in _TRANSIENT_ERRNOS or exc.errno is None:
                    frame_iter = None
                    time.sleep(0.001)
                    continue
                # Real OSError (EIO, ENODEV, etc.) — treat as fatal.
                self._emit_health(
                    HealthEvent(
                        stream_id=self.id,
                        kind=HealthEventKind.ERROR,
                        at_ns=time.monotonic_ns(),
                        detail=f"capture loop ended: {exc!r}",
                    )
                )
                return
            except Exception as exc:  # noqa: BLE001 - PyAV surfaces diverse errors here
                # Genuine non-OS error — emit a health event and exit.
                self._emit_health(
                    HealthEvent(
                        stream_id=self.id,
                        kind=HealthEventKind.ERROR,
                        at_ns=time.monotonic_ns(),
                        detail=f"capture loop ended: {exc!r}",
                    )
                )
                return

    # ------------------------------------------------------------------
    # Live preview
    # ------------------------------------------------------------------

    @property
    def latest_frame(self) -> Any:
        """Return the most recently captured BGR frame, or ``None``."""
        with self._frame_lock:
            return self._latest_frame

    def _release_av_resources(self) -> None:
        if self._encoder is not None:
            self._encoder.close()
            self._encoder = None
        if self._input is not None:
            try:
                self._input.close()
            finally:
                self._input = None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> list:
        """Enumerate attached UVC webcams.

        Uses the platform-native enumeration tool so the discovery pass
        is fast and avoids triggering the camera permission dialog:

        - macOS: ``system_profiler SPCameraDataType -json``
        - Linux: ``/dev/video*`` inspection
        - Other / fallback: nothing returned — use explicit
          ``UVCWebcamStream(device_index=...)`` construction.
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
    """macOS enumeration via ``system_profiler SPCameraDataType``."""
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
    """Linux enumeration via ``/dev/video*`` + optional name lookup."""
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
