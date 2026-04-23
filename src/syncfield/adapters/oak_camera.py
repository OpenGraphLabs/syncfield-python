"""OakCameraStream — DepthAI-based reference adapter for Luxonis OAK cameras.

Supports OAK-1, OAK-D, OAK-D Lite, OAK-D S2 and related devices through the
DepthAI v3 pipeline API. The adapter captures RGB frames to an MP4 file via
the shared :class:`~syncfield.adapters._video_encoder.VideoEncoder` (PyAV)
and, when ``depth_enabled=True``, also writes a raw uint16 depth stream
(little-endian, millimeters) to a sibling ``.depth.bin`` file.

Requires the ``oak`` optional extra:

    pip install syncfield[oak]     # depthai

The ``VideoEncoder`` dependency (PyAV) ships with the base package.

Lifecycle
---------

This adapter implements the 4-phase :class:`~syncfield.Stream` SPI so
the viewer can show live OAK preview frames **before** Record is
pressed:

* ``prepare()``       — create the output directory.
* ``connect()``       — discover the target device, build and start the
  DepthAI pipeline, spawn the capture thread in preview-only mode.
  ``latest_frame`` begins updating as soon as the pipeline warms up.
* ``start_recording()`` — open the MP4 (and optional depth binary)
  writer and flip the ``_recording`` flag so the running capture loop
  starts writing and emitting :class:`SampleEvent`\\ s.
* ``stop_recording()`` — flip the flag back off, close the writers,
  return the finalization report. The capture loop **keeps running**
  so preview stays live and the operator can start another recording
  without rebuilding the pipeline.
* ``disconnect()``    — signal the capture thread, join it, and
  release the DepthAI pipeline.

Legacy ``start()`` / ``stop()`` are still supported for 0.1-era code
paths — they collapse the new lifecycle into a one-shot
``connect + start_recording`` / ``stop_recording + disconnect`` pair.

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

from syncfield.adapters._video_encoder import (
    VideoEncoder,
    compute_jitter_percentiles,
    remux_h264_to_mp4,
)

from syncfield.clock import SessionClock
from syncfield.stream import StreamBase
from syncfield.types import (
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    StreamCapabilities,
)


def _device_timestamp_ns(msg: Any) -> Optional[int]:
    """Return the frame's device-clock timestamp as integer nanoseconds.

    ``msg.getTimestamp()`` is a ``datetime.timedelta`` anchored to the
    Myriad-X board's own clock (power-up relative). This helper returns
    the raw value — no attempt to project it onto the host monotonic
    clock, because DepthAI 3.x does not actually synchronise the two
    (the earlier ``device_shutter_host_ns`` path discovered ~12 day
    offsets between boards; cross-domain projection is unsafe).

    Downstream we use this value only for **inter-frame interval
    smoothing**: the deltas between consecutive frames' device clocks
    are jitter-free sensor cadence, which — combined with host arrival
    as the session anchor — removes host-side XLink/transport jitter
    from ``capture_ns`` without caring about absolute clock alignment.
    See ``SyncSession._refine_video_with_device_timestamps``.
    """
    if msg is None:
        return None
    try:
        td = msg.getTimestamp()
    except Exception:
        return None
    if td is None:
        return None
    # Integer arithmetic — avoid float rounding at ns magnitudes.
    return ((td.days * 86_400 + td.seconds) * 1_000_000 + td.microseconds) * 1_000


#: Supported RGB output encodings.
#:
#: * ``"h264"`` — on-device H.264 hardware encoder (default). Full-res
#:   frames travel over XLink as compressed bitstream, cutting USB
#:   bandwidth ~20-50x versus raw and eliminating the host-side PyAV
#:   encode on the capture thread. A separate low-res BGR branch feeds
#:   the viewer preview so ``latest_frame`` still updates in real time.
#: * ``"raw"`` — uncompressed BGR888p streamed over XLink and re-
#:   encoded host-side via PyAV. Retained as a fallback for environments
#:   where the on-device encoder is unavailable or when a caller wants
#:   direct access to BGR frames in the capture loop.
OAK_ENCODING_H264 = "h264"
OAK_ENCODING_RAW = "raw"
_OAK_ENCODINGS = (OAK_ENCODING_H264, OAK_ENCODING_RAW)


class OakCameraStream(StreamBase):
    """Captures RGB (and optional depth) from a Luxonis OAK camera.

    See the module docstring for the full 4-phase lifecycle. In short:

    * ``connect()`` builds and starts the DepthAI pipeline so
      :attr:`latest_frame` begins updating (live viewer preview).
    * ``start_recording(session_clock)`` opens the MP4 (and optional
      depth bin) writer and flips the ``_recording`` flag so the
      already-running capture loop begins writing and emitting samples.
    * ``stop_recording()`` closes the writers but leaves the pipeline
      running so preview stays live.
    * ``disconnect()`` tears down the pipeline.

    Args:
        id: Stream id (also used as the output file name: ``{id}.mp4``).
        output_dir: Directory for the MP4 (and optional depth) file.
        rgb_resolution: Desired RGB resolution as ``(width, height)``.
            Defaults to 720p — enough for most Physical AI benchmark
            and imitation-learning workloads while keeping the XLink
            budget small when two OAK boards share a USB bus. Pass
            ``(1920, 1080)`` to opt into 1080p.
        rgb_fps: Desired RGB frame rate.
        depth_enabled: If True, also capture raw depth to ``{id}.depth.bin``.
        depth_resolution: Depth resolution as ``(width, height)``. Must be a
            resolution supported by the device (e.g. ``(640, 400)``).
        depth_fps: Depth frame rate.
        encoding: ``"h264"`` (default) routes frames through the OAK's
            on-device H.264 hardware encoder so only compressed
            packets traverse XLink. ``"raw"`` falls back to the
            uncompressed BGR888p pipeline re-encoded host-side by
            PyAV — retained for debugging and environments without
            the on-device encoder.
        h264_bitrate_kbps: Target CBR bitrate for h264 mode. ``0``
            (default) lets DepthAI pick based on resolution + fps.
        h264_keyframe_interval: Insert an IDR keyframe every N frames.
            ``None`` (default) picks one keyframe per second (= fps).
        preview_resolution: Low-res BGR size used to feed the viewer
            preview in h264 mode. Does not affect the recorded MP4.
        preview_fps: Frame rate of the low-res preview branch.
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
        rgb_resolution: Tuple[int, int] = (1280, 720),
        rgb_fps: int = 30,
        depth_enabled: bool = False,
        depth_resolution: Tuple[int, int] = (640, 400),
        depth_fps: int = 30,
        encoding: str = OAK_ENCODING_H264,
        h264_bitrate_kbps: int = 0,
        h264_keyframe_interval: Optional[int] = None,
        preview_resolution: Tuple[int, int] = (320, 180),
        preview_fps: float = 10.0,
    ) -> None:
        if encoding not in _OAK_ENCODINGS:
            raise ValueError(
                f"OakCameraStream.encoding must be one of {_OAK_ENCODINGS!r}, "
                f"got {encoding!r}"
            )

        super().__init__(
            id=id,
            kind="video",
            capabilities=StreamCapabilities(
                provides_audio_track=False,  # OAK cameras have no audio
                supports_precise_timestamps=True,
                is_removable=True,
                produces_file=True,
                target_hz=float(rgb_fps),
            ),
        )
        self._output_dir = Path(output_dir)
        self._device_id = device_id
        self._rgb_resolution = rgb_resolution
        self._rgb_fps = rgb_fps
        self._depth_enabled = depth_enabled
        self._depth_resolution = depth_resolution
        self._depth_fps = depth_fps

        self._encoding = encoding
        self._h264_bitrate_kbps = int(h264_bitrate_kbps)
        # Default to one keyframe per second (== rgb_fps). Users that record
        # long clips and care about seek granularity can lower this.
        self._h264_keyframe_interval = (
            int(h264_keyframe_interval)
            if h264_keyframe_interval is not None
            else int(rgb_fps)
        )
        self._preview_resolution = preview_resolution
        self._preview_fps = float(preview_fps)

        # Pipeline + queue handles (populated in connect()).
        self._pipeline: Any = None
        # In h264 mode ``_q_rgb`` yields encoded packets from the
        # on-device VideoEncoder; in raw mode it yields full-res BGR
        # frames directly from the camera. The capture loop dispatches
        # on ``self._encoding`` to pick the right handler.
        self._q_rgb: Any = None
        self._q_preview: Any = None
        self._q_depth: Any = None

        # Recording state.
        self._mp4_path = self._output_dir / f"{id}.mp4"
        # Raw intermediate bitstream used only in h264 mode. Written to
        # disk during the recording window and remuxed into ``_mp4_path``
        # at ``stop_recording`` time. Removed on successful remux; kept
        # in place on failure so the footage is recoverable.
        self._h264_path = self._output_dir / f"{id}.h264"
        self._depth_path = self._output_dir / f"{id}.depth.bin"
        self._video_writer: Optional[VideoEncoder] = None
        self._h264_file: Any = None
        self._depth_file: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._frame_count = 0
        self._depth_frame_count = 0
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None

        # Jitter collection: inter-frame intervals (ns) over the current
        # recording window. Reset in start_recording().
        self._prev_capture_ns: Optional[int] = None
        self._intervals_ns: list[int] = []

        # True while the capture loop should write frames to disk and
        # emit ``SampleEvent``. ``connect()`` leaves this False so the
        # CONNECTED preview phase never touches the filesystem;
        # ``start_recording()`` flips it True; ``stop_recording()``
        # flips it False again while the capture thread keeps running.
        self._recording = False

        # Live preview support — the viewer reads ``latest_frame`` to render
        # the stream card thumbnail. ``_frame_lock`` protects handoff between
        # the capture thread and the reader.
        self._frame_lock = threading.Lock()
        self._latest_frame: Any = None

    # ------------------------------------------------------------------
    # Stream SPI — 4-phase lifecycle
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        """Validate the adapter is ready to connect.

        The heavy lifting — device discovery, pipeline build, pipeline
        start — happens in :meth:`connect` so the viewer can show a
        live preview as soon as the session enters the ``CONNECTED``
        state. ``prepare()`` stays cheap and idempotent.
        Output directory is created later in ``start_recording()``.
        """
        pass

    #: How many times to poll ``dai.Device.getAllAvailableDevices()``
    #: before giving up. XLink enumeration is asynchronous — on multi-
    #: board rigs the first probe often returns only a subset, and the
    #: rest appear up to several seconds later while a sibling board
    #: boots and the Mac USB stack re-quiesces. We've observed:
    #:
    #: * dual-OAK (USB-3 + USB-3 on different controllers): ~2 s window
    #: * dual-OAK involving OAK-D-Lite (USB-2-only board): 10–20 s
    #:   before the USB-2 board reappears in the enumeration list
    #: * triple-OAK with USB-2 hub sharing: even longer
    #:
    #: 24 s ceiling covers the worst case we've measured without blowing
    #: up the happy-path connect time — the probe returns on first hit
    #: when every board is already visible.
    _ENUMERATE_RETRIES = 16
    _ENUMERATE_RETRY_DELAY_S = 1.5

    def _locate_device(self) -> Any:
        """Find the target OAK, retrying the XLink enumeration if needed.

        ``dai.Device.getAllAvailableDevices()`` is an asynchronous probe
        over XLink. On dual-OAK rigs the first call frequently returns
        only one of the two boards, then 500–1000 ms later the second
        board appears. When the caller pinned a specific ``device_id``
        and it's missing from the first probe, we re-probe up to
        :attr:`_ENUMERATE_RETRIES` times before raising — that's the
        difference between a flaky startup and a hard failure the
        operator has to replug around.

        When ``device_id`` is ``None`` (pick any), we return on the
        first non-empty result so auto-pick stays fast.

        Raises:
            RuntimeError: If no device is found after exhausting all
                retries, or if the pinned ``device_id`` never appears.
        """
        last_seen: list = []
        for attempt in range(self._ENUMERATE_RETRIES):
            devices = dai.Device.getAllAvailableDevices()
            last_seen = devices
            if devices:
                if self._device_id is None:
                    return devices[0]
                for dev in devices:
                    if getattr(dev, "deviceId", None) == self._device_id:
                        return dev
            if attempt < self._ENUMERATE_RETRIES - 1:
                time.sleep(self._ENUMERATE_RETRY_DELAY_S)

        if not last_seen:
            raise RuntimeError(
                "No OAK devices found after "
                f"{self._ENUMERATE_RETRIES} enumeration attempts. Check "
                "cables, power, and that no other DepthAI process is "
                "holding the board."
            )
        available = [getattr(d, "deviceId", "?") for d in last_seen]
        raise RuntimeError(
            f"OAK device_id {self._device_id!r} not found after "
            f"{self._ENUMERATE_RETRIES} enumeration attempts. Visible "
            f"devices: {available}. If the missing device is physically "
            f"attached, unplug and replug its USB cable — Myriad-X "
            f"boards can enter a zombie state after an unclean shutdown."
        )

    def connect(self) -> None:
        """Open the DepthAI pipeline and spawn the preview capture thread.

        Discovers the requested device (by ``device_id`` if supplied,
        else the first attached OAK), builds the pipeline, starts it,
        and spawns the capture thread in **preview-only** mode:
        :attr:`latest_frame` updates continuously, but no file is
        written and no :class:`SampleEvent` is emitted until
        :meth:`start_recording` flips the ``_recording`` flag.

        Idempotent — calling ``connect()`` on an already-connected
        stream is a no-op so legacy callers that jump straight to
        ``start()`` don't spawn a second capture thread.

        Raises:
            RuntimeError: If no OAK devices are connected, or if the
                requested ``device_id`` is not among the currently
                attached devices after :attr:`_ENUMERATE_RETRIES`
                probe attempts.
        """
        self._install_depthai_bridge()

        if self._thread is not None and self._thread.is_alive():
            return

        selected = self._locate_device()

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

        # Reset counters so a reconnect starts clean.
        self._recording = False
        self._frame_count = 0
        self._depth_frame_count = 0
        self._first_at = None
        self._last_at = None
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, name=f"oak-{self.id}", daemon=True
        )
        self._thread.start()

    def start_recording(self, session_clock: SessionClock) -> None:
        """Open output files and flip the recording flag.

        The capture thread is already running from :meth:`connect`, so
        this is a writer construction plus a boolean flip — fast enough
        to run atomically across every stream in the orchestrator's
        start phase.

        In h264 mode the "writer" is just a raw file handle for the
        compressed bitstream; the MP4 container is synthesised at
        :meth:`stop_recording` time via :func:`remux_h264_to_mp4`.

        If the caller skipped :meth:`connect` (legacy 0.1 ``start()``
        path), the pipeline is started here first so the writer always
        has a feeder.
        """
        if self._thread is None or not self._thread.is_alive():
            self.connect()
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Reset per-recording counters so a second recording in the same
        # session starts clean.
        self._frame_count = 0
        self._depth_frame_count = 0
        self._first_at = None
        self._last_at = None
        self._prev_capture_ns = None
        self._intervals_ns = []

        if self._encoding == OAK_ENCODING_H264:
            # Drop any stale bitstream from a crashed prior session so
            # the capture loop never writes on top of foreign bytes.
            if self._h264_path.exists():
                try:
                    self._h264_path.unlink()
                except OSError:
                    pass
            self._h264_file = open(self._h264_path, "wb")
        else:
            width, height = self._rgb_resolution
            self._video_writer = VideoEncoder.open(
                self._mp4_path,
                width=width,
                height=height,
                fps=float(self._rgb_fps),
            )

        if self._depth_enabled:
            self._depth_file = open(self._depth_path, "wb")

        # Flip the flag LAST so the capture loop doesn't race into a
        # half-built writer.
        self._recording = True

    def stop_recording(self) -> FinalizationReport:
        """Flip recording off, close the writers, return the report.

        The pipeline stays live so the viewer preview keeps rendering
        and the operator can start a fresh recording on the same
        session without re-opening hardware.

        In h264 mode the raw ``.h264`` bitstream is remuxed into the
        target ``.mp4`` here (copy-mode, near-instant). If the remux
        fails the raw file is left in place so the footage is
        recoverable and a health event is emitted so the operator can
        see it in the session report.
        """
        self._recording = False
        self._release_writers()

        mp4_available = self._finalize_mp4()

        jitter_p95, jitter_p99 = compute_jitter_percentiles(self._intervals_ns)
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=self._frame_count,
            file_path=(
                self._mp4_path if self._frame_count > 0 and mp4_available else None
            ),
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=None,
            jitter_p95_ns=jitter_p95,
            jitter_p99_ns=jitter_p99,
        )

    def _finalize_mp4(self) -> bool:
        """Produce ``_mp4_path`` from the captured bitstream.

        * raw mode — the PyAV ``VideoEncoder`` already wrote the MP4
          during the recording window; nothing to do.
        * h264 mode — remux ``_h264_path`` into ``_mp4_path`` in copy
          mode.

        Returns ``True`` if an MP4 file is available on disk after
        this call, ``False`` if the remux failed. A failure is
        surfaced as a health event so it shows up in the session
        report without aborting the whole finalization.
        """
        if self._encoding != OAK_ENCODING_H264:
            return True
        if self._frame_count <= 0 or not self._h264_path.exists():
            return False
        try:
            remux_h264_to_mp4(
                self._h264_path,
                self._mp4_path,
                fps=float(self._rgb_fps),
            )
        except Exception as exc:  # noqa: BLE001 - PyAV surfaces diverse errors
            self._emit_health(
                HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.ERROR,
                    at_ns=time.monotonic_ns(),
                    detail=(
                        f"h264 → mp4 remux failed: {exc!r}; "
                        f"raw bitstream kept at {self._h264_path}"
                    ),
                )
            )
            return False
        # Remux succeeded — clean up the intermediate bitstream.
        try:
            self._h264_path.unlink()
        except OSError:
            pass
        return True

    def _install_depthai_bridge(self) -> None:
        """Install a logging.Handler that converts depthai native log records into HealthEvents.

        Idempotent. Registered on depthai's module logger so every internal
        warning/error that depthai emits during this stream's lifetime is
        routed to the IncidentTracker via :meth:`_emit_health`.
        """
        import logging as _logging
        from syncfield.health.detectors.depthai_bridge import DepthAILoggerBridge

        if getattr(self, "_depthai_bridge", None) is not None:
            return
        self._depthai_bridge = DepthAILoggerBridge(
            stream_id=self.id,
            sink=lambda _sid, ev: self._emit_health(ev),
        )
        _logging.getLogger("depthai").addHandler(self._depthai_bridge)

    def _uninstall_depthai_bridge(self) -> None:
        """Remove the depthai logging handler. Idempotent."""
        import logging as _logging
        bridge = getattr(self, "_depthai_bridge", None)
        if bridge is None:
            return
        _logging.getLogger("depthai").removeHandler(bridge)
        self._depthai_bridge = None

    def disconnect(self) -> None:
        """Stop the capture thread and release the DepthAI pipeline.

        Called when the session returns to ``IDLE``. Idempotent —
        calling twice is safe. After this call the adapter holds no
        DepthAI handles.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._release_pipeline()
        self._uninstall_depthai_bridge()

    # ------------------------------------------------------------------
    # Legacy one-shot lifecycle
    # ------------------------------------------------------------------

    def start(self, session_clock: SessionClock) -> None:
        """Legacy one-shot start — ``connect() + start_recording()``.

        Exists so 0.1-era scripts that call ``prepare() → start() →
        stop()`` keep working without changes. New callers (the
        viewer, the 4-phase orchestrator path) should use
        :meth:`connect` and :meth:`start_recording` directly to get
        live preview before the first Record click.
        """
        self.connect()
        self.start_recording(session_clock)

    def stop(self) -> FinalizationReport:
        """Legacy one-shot stop — ``stop_recording() + disconnect()``."""
        report = self.stop_recording()
        self.disconnect()
        return report

    # ------------------------------------------------------------------
    # Pipeline construction
    # ------------------------------------------------------------------

    def _build_pipeline(self) -> Any:
        """Build a DepthAI v3 pipeline with the requested outputs.

        Always creates an RGB ``Camera`` node. The shape of the RGB
        subgraph depends on :attr:`_encoding`:

        * ``"h264"`` — full-res NV12 output fans into a ``VideoEncoder``
          that emits compressed H.264 over XLink. A parallel low-res
          BGR branch feeds the viewer preview without paying the
          full-res bandwidth cost.
        * ``"raw"`` — legacy single-branch BGR888p stream straight out
          of the camera. The capture loop re-encodes host-side via
          PyAV.

        If ``depth_enabled`` is True, also creates a ``StereoDepth``
        node wired to the on-board mono cameras.
        """
        pipeline = dai.Pipeline()

        # --- RGB camera --------------------------------------------------
        cam = pipeline.create(dai.node.Camera)
        cam.build()

        if self._encoding == OAK_ENCODING_H264:
            self._build_h264_branch(pipeline, cam)
        else:
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

    def _build_h264_branch(self, pipeline: Any, cam: Any) -> None:
        """Wire Camera → VideoEncoder (full-res, compressed) plus a
        low-res BGR preview branch used only for ``latest_frame``.

        Must be called exactly once from :meth:`_build_pipeline` when
        ``encoding == "h264"``. Populates ``self._q_rgb`` with the
        encoded-bitstream queue and ``self._q_preview`` with the live
        preview queue.
        """
        # Full-resolution NV12 feed for the hardware encoder. NV12 is
        # the encoder's native pixel layout; requesting BGR here would
        # force an on-device CSC and lose the bandwidth win.
        encoder_in = cam.requestOutput(
            self._rgb_resolution,
            dai.ImgFrame.Type.NV12,
            fps=float(self._rgb_fps),
        )
        encoder = pipeline.create(dai.node.VideoEncoder)
        encoder.build(
            input=encoder_in,
            frameRate=float(self._rgb_fps),
            profile=dai.VideoEncoderProperties.Profile.H264_MAIN,
            keyframeFrequency=self._h264_keyframe_interval,
        )
        if self._h264_bitrate_kbps > 0:
            encoder.setBitrateKbps(self._h264_bitrate_kbps)
        self._q_rgb = encoder.out.createOutputQueue()

        # Low-res BGR feed for the viewer. At 320x180×10fps this costs
        # ~1.7 MB/s per camera — negligible compared to the old
        # 1080p×30fps BGR firehose that saturated USB.
        preview_out = cam.requestOutput(
            self._preview_resolution,
            dai.ImgFrame.Type.BGR888p,
            fps=self._preview_fps,
        )
        self._q_preview = preview_out.createOutputQueue()

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        """Body of the background thread — tight read/timestamp/write loop.

        Two phases, distinguished by the ``_recording`` flag:

        * **Preview** (``_recording == False``) — publish every frame
          to ``latest_frame`` so the viewer card stays live, but do
          **not** write to the MP4, update frame counters, or emit
          ``SampleEvent``. The capture runs continuously from the
          moment :meth:`connect` spawned this thread.
        * **Recording** (``_recording == True``) — same preview
          publish, plus write to the on-disk bitstream (h264 mode) or
          MP4 via PyAV (raw mode), advance ``_frame_count``, drain a
          depth tick, and emit ``SampleEvent``. The capture timestamp
          is sampled *immediately* after ``queue.get()`` so the jitter
          between the physical frame and the recorded monotonic time
          stays as small as possible.

        The loop exits when ``_stop_event`` fires (from
        :meth:`disconnect`).
        """
        while not self._stop_event.is_set():
            rgb_msg = self._safe_get_rgb()
            capture_ns = time.monotonic_ns()

            # In h264 mode the viewer preview rides a separate low-res
            # BGR branch — poll it opportunistically so ``latest_frame``
            # keeps updating even if the main encoded queue is quiet.
            if self._encoding == OAK_ENCODING_H264:
                self._drain_preview_tick()

            if rgb_msg is None:
                continue

            # Device-clock timestamp (raw Myriad-X ns since board power-up).
            # Pulled here — *before* any handler touches the message — so the
            # value travels alongside ``capture_ns`` to the orchestrator.
            # Downstream device-interval smoothing in ``SyncSession`` uses
            # the deltas between consecutive frames' device clocks to scrub
            # host-arrival jitter out of the recorded ``capture_ns``.
            device_ts_ns = _device_timestamp_ns(rgb_msg)

            # Recording-window-only jitter collection (see UVC adapter for rationale).
            if self._recording:
                if self._prev_capture_ns is not None:
                    self._intervals_ns.append(capture_ns - self._prev_capture_ns)
                self._prev_capture_ns = capture_ns

            if self._encoding == OAK_ENCODING_H264:
                self._handle_encoded_packet(rgb_msg, capture_ns, device_ts_ns)
            else:
                self._handle_raw_frame(rgb_msg, capture_ns, device_ts_ns)

            if self._recording and self._depth_enabled:
                self._drain_depth_tick()

    def _handle_encoded_packet(
        self,
        msg: Any,
        capture_ns: int,
        device_ts_ns: Optional[int],
    ) -> None:
        """h264 mode — write the on-device encoded packet to the raw
        ``.h264`` file and emit a :class:`SampleEvent`.

        The packet payload is Annex-B H.264 with SPS/PPS inline, so the
        raw file is directly decodable and can be remuxed into an MP4
        at ``stop_recording`` time with no re-encoding.
        """
        if not self._recording:
            return
        if self._first_at is None:
            self._first_at = capture_ns
        self._last_at = capture_ns
        self._frame_count += 1
        if self._h264_file is not None:
            # ``getData()`` returns a depthai ``VectorUChar``; ``bytes(...)``
            # gives us a plain buffer the OS write path prefers.
            self._h264_file.write(bytes(msg.getData()))
        channels = (
            {"device_timestamp_ns": device_ts_ns} if device_ts_ns is not None else None
        )
        self._emit_sample(
            SampleEvent(
                stream_id=self.id,
                frame_number=self._frame_count - 1,
                capture_ns=capture_ns,
                channels=channels,
            )
        )

    def _handle_raw_frame(
        self,
        msg: Any,
        capture_ns: int,
        device_ts_ns: Optional[int],
    ) -> None:
        """raw mode — publish the BGR frame for preview and host-encode
        via PyAV.
        """
        frame = msg.getCvFrame()
        with self._frame_lock:
            self._latest_frame = frame

        if not self._recording:
            return
        if self._first_at is None:
            self._first_at = capture_ns
        self._last_at = capture_ns
        self._frame_count += 1
        if self._video_writer is not None:
            self._video_writer.write(frame)
        channels = (
            {"device_timestamp_ns": device_ts_ns} if device_ts_ns is not None else None
        )
        self._emit_sample(
            SampleEvent(
                stream_id=self.id,
                frame_number=self._frame_count - 1,
                capture_ns=capture_ns,
                channels=channels,
            )
        )

    def _drain_preview_tick(self) -> None:
        """h264 mode only — non-blocking pull from the low-res BGR
        preview queue. Publishes the most recent frame to
        :attr:`latest_frame` for the viewer.
        """
        if self._q_preview is None:
            return
        try:
            msg = self._q_preview.tryGet()
        except Exception:
            return
        if msg is None:
            return
        try:
            frame = msg.getCvFrame()
        except Exception:
            return
        with self._frame_lock:
            self._latest_frame = frame

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
                self._video_writer.close()
            except Exception:
                pass
            self._video_writer = None
        if self._h264_file is not None:
            try:
                self._h264_file.flush()
                self._h264_file.close()
            except Exception:
                pass
            self._h264_file = None
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
        self._q_preview = None
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
