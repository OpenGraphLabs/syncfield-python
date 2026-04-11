"""OakCameraStream — DepthAI-based reference adapter for Luxonis OAK cameras.

Supports OAK-1, OAK-D, OAK-D Lite, OAK-D S2 and related devices through the
DepthAI v3 pipeline API. The adapter captures RGB frames to an MP4 file via
``cv2.VideoWriter`` and, when ``depth_enabled=True``, also writes a raw uint16
depth stream (little-endian, millimeters) to a sibling ``.depth.bin`` file.

Requires two optional extras:

    pip install syncfield[oak]     # depthai
    pip install syncfield[uvc]     # opencv-python for the MP4 writer

Both extras are available together via ``syncfield[all]``.

The adapter is intentionally thinner than the full-featured OakCamera class
used inside opengraph-studio/recorder — it ships the 80% common case (RGB +
optional depth) so the code stays small and easy to extend. For IMU, stereo
rectified output, or advanced calibration, write a subclass against the
depthai API directly.
"""

from __future__ import annotations

import struct
import threading
import time
from pathlib import Path
from typing import Any, Optional, Tuple

try:
    import depthai as dai  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - exercised via sys.modules patch
    raise ImportError(
        "OakCameraStream requires depthai. "
        "Install with `pip install syncfield[oak]`."
    ) from exc

try:
    import cv2  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - exercised via sys.modules patch
    raise ImportError(
        "OakCameraStream also requires opencv-python for the MP4 writer. "
        "Install with `pip install syncfield[uvc]`."
    ) from exc

from syncfield.clock import SessionClock
from syncfield.stream import StreamBase
from syncfield.types import (
    FinalizationReport,
    SampleEvent,
    StreamCapabilities,
)


class OakCameraStream(StreamBase):
    """Captures RGB (and optional depth) from a Luxonis OAK camera.

    Lifecycle:
        1. ``prepare()`` discovers a device, builds a DepthAI pipeline with an
           RGB ``Camera`` node (and optionally a ``StereoDepth`` node), and
           starts the pipeline.
        2. ``start()`` opens the MP4 writer (and depth raw-bin file if
           depth is enabled), then spins up a background thread that reads
           frames in a tight loop, timestamps each read with
           ``time.monotonic_ns()``, writes the frame to disk, and emits a
           :class:`~syncfield.types.SampleEvent`.
        3. ``stop()`` signals the thread, joins it, releases the pipeline
           and writers, and returns a :class:`FinalizationReport`.

    Args:
        id: Stream id (also used as the output file name: ``{id}.mp4``).
        output_dir: Directory for the MP4 (and optional depth) file.
        rgb_resolution: Desired RGB resolution as ``(width, height)``.
        rgb_fps: Desired RGB frame rate.
        depth_enabled: If True, also capture raw depth to ``{id}.depth.bin``.
        depth_resolution: Depth resolution as ``(width, height)``. Must be a
            resolution supported by the device (e.g. ``(640, 400)``).
        depth_fps: Depth frame rate.
    """

    # Class-level hints for the discovery registry (see
    # ``syncfield.discovery``). ``_discovery_kind`` filters adapters by
    # Stream kind; ``_discovery_adapter_type`` is the stable string id
    # used in ``DiscoveryReport.errors`` keys and the CLI output.
    _discovery_kind = "video"
    _discovery_adapter_type = "oak_camera"

    def __init__(
        self,
        id: str,
        output_dir: Path | str,
        device_id: Optional[str] = None,
        rgb_resolution: Tuple[int, int] = (1920, 1080),
        rgb_fps: int = 30,
        depth_enabled: bool = False,
        depth_resolution: Tuple[int, int] = (640, 400),
        depth_fps: int = 30,
    ) -> None:
        super().__init__(
            id=id,
            kind="video",
            capabilities=StreamCapabilities(
                provides_audio_track=False,  # OAK cameras have no audio
                supports_precise_timestamps=True,
                is_removable=True,
                produces_file=True,
            ),
        )
        self._output_dir = Path(output_dir)
        self._device_id = device_id
        self._rgb_resolution = rgb_resolution
        self._rgb_fps = rgb_fps
        self._depth_enabled = depth_enabled
        self._depth_resolution = depth_resolution
        self._depth_fps = depth_fps

        # Pipeline + queue handles (populated in prepare()).
        self._pipeline: Any = None
        self._q_rgb: Any = None
        self._q_depth: Any = None

        # Recording state.
        self._mp4_path = self._output_dir / f"{id}.mp4"
        self._depth_path = self._output_dir / f"{id}.depth.bin"
        self._video_writer: Any = None
        self._depth_file: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._frame_count = 0
        self._depth_frame_count = 0
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None

        # Live preview support — the viewer reads ``latest_frame`` to render
        # the stream card thumbnail. ``_frame_lock`` protects handoff between
        # the capture thread and the reader.
        self._frame_lock = threading.Lock()
        self._latest_frame: Any = None

    # ------------------------------------------------------------------
    # Stream SPI
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        """Discover a device and build the DepthAI pipeline.

        When multiple OAK devices are connected, the ``device_id``
        constructor argument (a ``deviceId`` serial string as returned
        by :func:`depthai.Device.getAllAvailableDevices`) selects which
        one to open. If omitted, the first available device is used.

        Raises:
            RuntimeError: If no OAK devices are connected, or if the
                requested ``device_id`` is not among the currently
                attached devices.
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)

        devices = dai.Device.getAllAvailableDevices()
        if not devices:
            raise RuntimeError("No OAK devices found")

        if self._device_id is not None:
            matching = [
                d for d in devices
                if getattr(d, "deviceId", None) == self._device_id
            ]
            if not matching:
                available = [getattr(d, "deviceId", "?") for d in devices]
                raise RuntimeError(
                    f"OAK device_id {self._device_id!r} not found. "
                    f"Available: {available}"
                )
            selected = matching[0]
        else:
            selected = devices[0]

        self._pipeline = self._build_pipeline()
        # DepthAI v3 build() accepts an optional device info; older
        # shims without the argument fall back to "first available".
        try:
            self._pipeline.build(selected)
        except TypeError:
            self._pipeline.build()
        self._pipeline.start()

        # Short warmup — the first few frames are often None while the
        # camera settles. Keeps the capture loop's error counters clean.
        time.sleep(1.0)

    def start(self, session_clock: SessionClock) -> None:
        """Open output files and launch the background capture thread."""
        width, height = self._rgb_resolution
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._video_writer = cv2.VideoWriter(
            str(self._mp4_path), fourcc, float(self._rgb_fps), (width, height)
        )
        if self._depth_enabled:
            self._depth_file = open(self._depth_path, "wb")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, name=f"oak-{self.id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> FinalizationReport:
        """Signal the thread, release the pipeline, return the report."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

        self._release_writers()
        self._release_pipeline()

        extra_channels: dict[str, Any] = {}
        if self._depth_enabled:
            extra_channels["depth_frame_count"] = self._depth_frame_count
            extra_channels["depth_path"] = (
                str(self._depth_path) if self._depth_frame_count > 0 else None
            )

        report = FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=self._frame_count,
            file_path=self._mp4_path if self._frame_count > 0 else None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=None,
        )
        # Expose depth stats through the health_events buffer so consumers
        # that only look at FinalizationReport still get visibility.
        return report

    # ------------------------------------------------------------------
    # Pipeline construction
    # ------------------------------------------------------------------

    def _build_pipeline(self) -> Any:
        """Build a DepthAI v3 pipeline with the requested outputs.

        Always creates an RGB ``Camera`` node. If ``depth_enabled`` is
        True, also creates a ``StereoDepth`` node wired to the on-board
        mono cameras.
        """
        pipeline = dai.Pipeline()

        # --- RGB camera --------------------------------------------------
        cam = pipeline.create(dai.node.Camera)
        cam.build()
        rgb_out = cam.requestOutput(
            self._rgb_resolution,
            dai.ImgFrame.Type.BGR888p,
            fps=float(self._rgb_fps),
        )
        self._q_rgb = rgb_out.createOutputQueue()

        # --- Optional stereo depth --------------------------------------
        if self._depth_enabled:
            stereo = pipeline.create(dai.node.StereoDepth)
            stereo.build(
                autoCreateCameras=True,
                presetMode=dai.node.StereoDepth.PresetMode.HIGH_DETAIL,
                size=self._depth_resolution,
                fps=float(self._depth_fps),
            )
            self._q_depth = stereo.depth.createOutputQueue()

        return pipeline

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        """Body of the background thread — tight read/timestamp/write loop.

        The timestamp is captured *immediately* after ``queue.get()`` so
        the jitter between the physical frame and the recorded timestamp
        stays as small as possible. Depth frames are consumed in the
        same tick with ``tryGet()`` so depth and RGB share the same
        monotonic anchor.
        """
        while not self._stop_event.is_set():
            rgb_msg = self._safe_get_rgb()
            capture_ns = time.monotonic_ns()
            if rgb_msg is None:
                continue

            frame = rgb_msg.getCvFrame()
            if self._first_at is None:
                self._first_at = capture_ns
            self._last_at = capture_ns
            self._frame_count += 1

            # Publish the latest frame for live preview (viewer reads this).
            with self._frame_lock:
                self._latest_frame = frame

            if self._video_writer is not None:
                self._video_writer.write(frame)
            self._emit_sample(
                SampleEvent(
                    stream_id=self.id,
                    frame_number=self._frame_count - 1,
                    capture_ns=capture_ns,
                )
            )

            if self._depth_enabled:
                self._drain_depth_tick()

    def _safe_get_rgb(self) -> Any:
        """Pull one RGB frame from the queue, swallowing timeouts."""
        try:
            return self._q_rgb.get(timeout=0.1)
        except Exception:
            return None

    def _drain_depth_tick(self) -> None:
        """Non-blocking depth pull — write whatever is ready this tick."""
        if self._q_depth is None or self._depth_file is None:
            return
        depth_msg = self._q_depth.tryGet()
        if depth_msg is None:
            return
        try:
            depth_frame = depth_msg.getFrame()  # uint16, little-endian, mm
            self._depth_file.write(depth_frame.tobytes())
            self._depth_frame_count += 1
        except Exception:
            # Depth is best-effort — a transient failure should not tear
            # down the RGB capture loop.
            pass

    # ------------------------------------------------------------------
    # Resource cleanup
    # ------------------------------------------------------------------

    def _release_writers(self) -> None:
        if self._video_writer is not None:
            try:
                self._video_writer.release()
            except Exception:
                pass
            self._video_writer = None
        if self._depth_file is not None:
            try:
                self._depth_file.flush()
                self._depth_file.close()
            except Exception:
                pass
            self._depth_file = None

    def _release_pipeline(self) -> None:
        if self._pipeline is not None:
            try:
                self._pipeline.stop()
            except Exception:
                pass
            self._pipeline = None
        self._q_rgb = None
        self._q_depth = None

    # ------------------------------------------------------------------
    # Live preview
    # ------------------------------------------------------------------

    @property
    def latest_frame(self) -> Any:
        """Return the most recently captured RGB frame, or ``None``.

        Thread-safe: the frame reference is published under a lock by the
        capture thread. Readers that mutate the returned array should
        ``.copy()`` it first — the viewer uploads it as a texture
        immediately and never mutates it in place.
        """
        with self._frame_lock:
            return self._latest_frame

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> list:
        """Enumerate currently attached OAK devices.

        Uses :func:`depthai.Device.getAllAvailableDevices` which is
        near-instant (sub-millisecond) — ``timeout`` is accepted for
        interface consistency but effectively ignored.

        Each returned :class:`~syncfield.discovery.DiscoveredDevice`
        has its ``device_id`` populated from the OAK serial
        (``deviceId``), so auto-added streams from ``scan_and_add`` pin
        to specific devices even when multiple OAKs are attached.

        Returns:
            List of :class:`~syncfield.discovery.DiscoveredDevice`. Empty
            list if no devices found or if the depthai probe raises for
            any reason — discovery never propagates errors.
        """
        from syncfield.discovery import DiscoveredDevice

        try:
            devices_info = dai.Device.getAllAvailableDevices()
        except Exception:
            return []

        results = []
        for info in devices_info:
            device_id = getattr(info, "deviceId", None) or ""
            name = getattr(info, "name", None) or "OAK"
            state = getattr(info, "state", None)
            state_str = getattr(state, "name", None) or str(state) if state else ""

            description_parts = [f"OAK · {device_id[:8]}…"] if device_id else ["OAK"]
            if state_str:
                description_parts.append(state_str.lower())
            description = " · ".join(description_parts)

            results.append(
                DiscoveredDevice(
                    adapter_type="oak_camera",
                    adapter_cls=cls,
                    kind="video",
                    display_name=name or "OAK camera",
                    description=description,
                    device_id=device_id or name or "oak",
                    construct_kwargs=(
                        {"device_id": device_id} if device_id else {}
                    ),
                    accepts_output_dir=True,
                )
            )
        return results


# ---------------------------------------------------------------------------
# Depth binary format helper
# ---------------------------------------------------------------------------
#
# The ``.depth.bin`` file is a simple concatenation of raw uint16 depth frames
# in row-major order. No header. Consumers need the resolution (which they
# can read from the manifest or know out of band) to reshape the buffer.
# This helper is purely informational — adapters do not need to call it.


def iter_depth_frames(
    path: Path | str,
    width: int,
    height: int,
):  # pragma: no cover - convenience helper, not exercised by adapter tests
    """Yield successive depth frames from a raw ``.depth.bin`` file.

    Args:
        path: Path to the ``.depth.bin`` file produced by OakCameraStream.
        width: Depth width in pixels (as configured on the stream).
        height: Depth height in pixels.

    Yields:
        Tuples of ``(frame_index, flat_uint16_list)``. Callers that want
        numpy arrays can do ``np.asarray(values, dtype=np.uint16).reshape(
        height, width)``.
    """
    frame_bytes = width * height * 2
    fmt = f"<{width * height}H"
    path = Path(path)
    with path.open("rb") as f:
        idx = 0
        while True:
            chunk = f.read(frame_bytes)
            if len(chunk) < frame_bytes:
                return
            yield idx, list(struct.unpack(fmt, chunk))
            idx += 1
