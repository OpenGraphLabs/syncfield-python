"""Full OAK-D Pro capture adapter using DepthAI's stable multi-device API.

One physical OAK is modeled as one composite stream: a single ``dai.Device``
and pipeline capture RGB (CAM_A), the stereo mono pair (CAM_B/CAM_C), and the
IMU together, so the desktop reconcile loop never has to coordinate multiple
handles to the same board. Artifacts share the RGB stem:

* ``{id}.mp4`` + orchestrator-written ``{id}.timestamps.jsonl`` — RGB
* ``{id}.left.mp4`` / ``{id}.right.mp4`` + stream-written
  ``{id}.{side}.timestamps.jsonl`` — stereo monos
* ``{id}.imu.jsonl`` — one row per synced IMU packet (accel + gyro + quat)

Hardware constraint (measured on OAK-D Pro-W, RVC2, USB3): the RVC2 ISP
sustains ~47 MP/s across all cameras. A full 30 fps on the stereo mono pair is
the priority for our data, so the defaults capture mono 1024x640 (640p at the
sensor's native 16:10) with RGB at 640x400, all three at 30 fps. Measured facts
on real hardware (10 s recordings):

* mono 1024x640 x2 @ 30 fps + RGB 640x400 @ 30 fps = clean, 0 drops (~47 MP/s,
  at the ceiling but stable — reproducible).
* mono 1152x720 x2 @ 30 fps = ~50 MP/s FOR THE MONO PAIR ALONE, over the
  ceiling — delivers an irregular ~24-26 fps WITH drops even with RGB off. So
  720p @ 30 fps is NOT attainable on this chip; 720p tops out at a clean 24 fps.
* All streams share ONE frame rate: a mismatched RGB rate (e.g. 10 fps) desyncs
  the ISP interleave and makes the mono pair jitter/drop, so matched rates
  matter more than shaving RGB frames.

Trade frame rate for resolution by editing the defaults: mono 1152x720 @ 24
(all rates matched) is clean if you need 720p; raising ``rgb_resolution`` or
mono fps past this budget pushes the mono pair into drops. Mono and IMU capture
require the DepthAI v3 API; the legacy (v2) pipeline path stays RGB-only.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import depthai as dai  # type: ignore[import-not-found]
import logging
from syncfield.adapters._video_encoder import (  # type: ignore[reportMissingImports]
    compute_jitter_percentiles,
    remux_h264_to_mp4,
)
from syncfield.clock import SessionClock  # type: ignore[reportMissingImports]
from syncfield.stream import StreamBase  # type: ignore[reportMissingImports]
from syncfield.types import (  # type: ignore[reportMissingImports]
    FinalizationReport,
    SampleEvent,
    StreamCapabilities,
)

logger = logging.getLogger(__name__)

_SAMPLE_EVENT_SUPPORTS_DEVICE_NS = "device_ns" in signature(SampleEvent).parameters


@dataclass(frozen=True)
class OakSubstream:
    """Descriptor for a derived (non-orchestrator) stream of a composite OAK.

    Substreams are views onto the composite stream's pipeline — they share
    its device handle and lifecycle and are surfaced to the UI by the preview
    backend, but they are not independently connectable.
    """

    id: str
    kind: str  # "video" | "sensor"
    label: str
    expected_hz: float | None = None


@dataclass(frozen=True)
class OakArtifact:
    """A recorded auxiliary file the composite produced besides its primary.

    The SDK orchestrator only knows the composite as one stream, so its
    ``manifest.json`` lists only the primary (RGB). The desktop backend folds
    these descriptors into the manifest after ``stop()`` so the sync pipeline
    discovers and aligns every OAK stream (left/right mono, IMU), not just RGB.
    """

    stream_id: str
    kind: str  # "video" | "sensor"
    path: Path
    frame_count: int


class _H264Recorder:
    """Recording state for one on-device H.264 encoder output.

    The primary (RGB) recorder leaves ``timestamps_path`` unset — its
    per-frame timestamps flow through ``SampleEvent`` into the orchestrator's
    ``{id}.timestamps.jsonl``. Aux recorders write their own timestamps file
    because the orchestrator only knows about the composite stream's primary
    samples.
    """

    def __init__(
        self,
        *,
        label: str,
        is_primary: bool = False,
    ) -> None:
        self.label = label
        # Paths are (re)bound from the composite's _output_dir at every
        # start_recording so consecutive recordings follow the episode dir.
        self.h264_path: Path = Path()
        self.mp4_path: Path = Path()
        self.timestamps_path: Path | None = None
        self.is_primary = is_primary
        self.queue: Any | None = None
        self.file: Any | None = None
        self.timestamps_file: Any | None = None
        self.sps: bytes | None = None
        self.pps: bytes | None = None
        self.started_on_keyframe = False
        self.frame_count = 0
        self.first_at: int | None = None
        self.last_at: int | None = None
        self.prev_capture_ns: int | None = None
        self.intervals_ns: list[int] = []
        # Rolling packet-arrival timestamps for the live capture-rate readout,
        # tracked in preview AND recording (unlike frame_count, which only
        # counts recorded frames). Guarded because the API thread reads it.
        self._recent_lock = threading.Lock()
        self._recent_ns: deque[int] = deque(maxlen=90)

    def observe_capture(self, capture_ns: int) -> None:
        with self._recent_lock:
            self._recent_ns.append(capture_ns)

    def capture_hz(self) -> float:
        with self._recent_lock:
            recent = tuple(self._recent_ns)
        if len(recent) < 2:
            return 0.0
        span_s = (recent[-1] - recent[0]) / 1_000_000_000
        return (len(recent) - 1) / span_s if span_s > 0 else 0.0

    def remember_parameter_sets(self, data: bytes) -> None:
        for nal_type, nal in _iter_h264_annex_b_nalus(data):
            if nal_type == 7:
                self.sps = nal
            elif nal_type == 8:
                self.pps = nal

    def parameter_sets(self) -> bytes:
        if self.sps is None or self.pps is None:
            return b""
        return self.sps + self.pps

    def begin_recording(self) -> None:
        self.frame_count = 0
        self.first_at = None
        self.last_at = None
        self.prev_capture_ns = None
        self.intervals_ns = []
        self.started_on_keyframe = False
        if not self.is_primary and self.queue is None:
            return
        if self.h264_path.exists():
            try:
                self.h264_path.unlink()
            except OSError:
                pass
        self.file = open(self.h264_path, "wb")
        if self.timestamps_path is not None:
            self.timestamps_file = open(self.timestamps_path, "w", encoding="utf-8")

    def write_frame(self, data: bytes, capture_ns: int, device_ts_ns: int | None) -> bool:
        """Append one encoded packet; returns False until the IDR gate opens."""
        if not self.started_on_keyframe:
            if not _contains_h264_nal_type(data, 5):
                return False
            parameter_sets = self.parameter_sets()
            if not parameter_sets:
                return False
            if self.file is not None and not _contains_h264_parameter_sets(data):
                self.file.write(parameter_sets)
            self.started_on_keyframe = True

        if self.first_at is None:
            self.first_at = capture_ns
        self.last_at = capture_ns
        if self.prev_capture_ns is not None:
            self.intervals_ns.append(capture_ns - self.prev_capture_ns)
        self.prev_capture_ns = capture_ns

        frame_number = self.frame_count
        self.frame_count += 1
        if self.file is not None:
            self.file.write(data)
        if self.timestamps_file is not None:
            self.timestamps_file.write(
                json.dumps(
                    {
                        "frame_number": frame_number,
                        "capture_ns": capture_ns,
                        "clock_source": "host_monotonic",
                        "device_timestamp_ns": device_ts_ns,
                    }
                )
                + "\n"
            )
        return True

    def close_files(self) -> None:
        if self.file is not None:
            try:
                self.file.flush()
                self.file.close()
            finally:
                self.file = None
        if self.timestamps_file is not None:
            try:
                self.timestamps_file.flush()
                self.timestamps_file.close()
            finally:
                self.timestamps_file = None

    def finish_recording(self, fps: float) -> str | None:
        """Close + remux this camera's capture; returns an error string or None.

        Only meaningful for recorders whose queue was live during the
        recording window (callers skip inactive ones).
        """
        self.close_files()
        if self.frame_count <= 0 or not self.h264_path.exists():
            return f"{self.label}: no decodable H.264 keyframe arrived during recording."
        try:
            remux_h264_to_mp4(self.h264_path, self.mp4_path, fps=fps)
        except Exception as exc:  # noqa: BLE001
            return f"{self.label}: H.264 remux failed: {exc}"
        if not (self.mp4_path.exists() and self.mp4_path.stat().st_size > 0):
            try:
                if self.mp4_path.exists():
                    self.mp4_path.unlink()
            except OSError:
                pass
            return f"{self.label}: remuxed MP4 is empty."
        try:
            self.h264_path.unlink()
        except OSError:
            pass
        return None


class _ImuRecorder:
    """JSONL sink for synced IMU packets (accel + gyro + rotation vector).

    Written from the dedicated IMU thread and closed from the main thread, so
    file access is guarded by a lock to avoid writing to a handle another
    thread is closing.
    """

    def __init__(self) -> None:
        # Rebound from the composite's _output_dir at every start_recording.
        self.path: Path = Path()
        self.queue: Any | None = None
        self.file: Any | None = None
        self.sample_count = 0
        self._lock = threading.Lock()

    def begin_recording(self) -> None:
        if self.queue is None:
            self.sample_count = 0
            return
        with self._lock:
            self.sample_count = 0
            if self.path.exists():
                try:
                    self.path.unlink()
                except OSError:
                    pass
            self.file = open(self.path, "w", encoding="utf-8")

    def write_row(self, row: dict[str, Any]) -> bool:
        """Append one canonical SensorSample row; returns whether it landed."""
        with self._lock:
            if self.file is None:
                return False
            self.file.write(json.dumps(row) + "\n")
            self.sample_count += 1
            return True

    def close_file(self) -> None:
        with self._lock:
            if self.file is not None:
                try:
                    self.file.flush()
                    self.file.close()
                finally:
                    self.file = None


class OakCameraStream(StreamBase):
    """Full-stream OAK-D Pro capture: RGB + stereo mono pair + IMU.

    One physical OAK is captured through a single ``dai.Device`` and pipeline:
    RGB (CAM_A) 400p30 H.264, the stereo mono pair (CAM_B/CAM_C) 640p30 H.264
    (1024x640, native 16:10), and the BNO086 IMU. See the module docstring for
    the bandwidth rationale (30 fps priority; 720p mono would cap at 24 fps on
    RVC2). The three cameras are hardware frame-synced (FSYNC), so
    every stream's frame N shares a device timestamp to ~0.02 ms; per-frame
    device timestamps are recorded for downstream device-clock alignment. Full
    factory calibration is read from EEPROM and written to
    ``{id}.calibration.json``.

    Presence of the stereo pair and IMU is feature-detected per device, so OAK
    models without them keep working as RGB-only. Aux stream failures never
    fail an otherwise-good RGB recording — they downgrade the finalization
    status to ``"partial"`` naming the losses. For the RGB + optional-depth
    minimal adapter, use :class:`OakRgbDepthStream` instead.
    """

    #: Discovery metadata (registered via ``syncfield.discovery``).
    _discovery_kind = "video"
    _discovery_adapter_type = "oak_camera"

    def __init__(
        self,
        id: str,
        output_dir: Path | str,
        device_id: str | None = None,
        rgb_fps: int = 30,
        rgb_resolution: tuple[int, int] = (640, 400),
        h264_bitrate_kbps: int = 8_000,
        preview_resolution: tuple[int, int] = (320, 200),
        mono_resolution: tuple[int, int] = (1024, 640),
        mono_fps: int = 30,
        mono_bitrate_kbps: int = 4_000,
        imu_rate_hz: int = 400,
        enable_mono: bool = True,
        enable_imu: bool = True,
        enable_mono_preview: bool = True,
        rgb_preview_from_h264: bool = False,
        **_: Any,
    ) -> None:
        super().__init__(
            id=id,
            kind="video",
            capabilities=StreamCapabilities(
                provides_audio_track=False,
                supports_precise_timestamps=True,
                is_removable=True,
                produces_file=True,
                target_hz=float(rgb_fps),
            ),
        )
        self._output_dir = Path(output_dir)
        self._device_id = device_id
        self._rgb_fps = int(rgb_fps)
        self._rgb_resolution = rgb_resolution
        self._h264_bitrate_bps = int(h264_bitrate_kbps) * 1000
        self._preview_resolution = preview_resolution
        self._mono_resolution = mono_resolution
        self._mono_fps = int(mono_fps)
        self._mono_bitrate_bps = int(mono_bitrate_kbps) * 1000
        self._imu_rate_hz = int(imu_rate_hz)
        self._enable_mono = enable_mono
        self._enable_imu = enable_imu
        # Preview outputs are extra ISP downscales on the cameras — see the
        # module docstring's bandwidth budget. On an ISP-bound config (e.g. a
        # Pi kiosk recording RGB at high resolution), they steal from capture.
        # ``enable_mono_preview=False`` skips the mono downscales (a kiosk that
        # only shows the RGB view never uses them). ``rgb_preview_from_h264``
        # drops the RGB ISP preview output too and instead decodes the encoded
        # H.264 stream host-side for the preview, so recording keeps the ISP to
        # itself and stays at the encode-only budget.
        self._enable_mono_preview = enable_mono_preview
        self._rgb_preview_from_h264 = rgb_preview_from_h264

        self._rgb = _H264Recorder(label="rgb", is_primary=True)
        self._left = _H264Recorder(label="left")
        self._right = _H264Recorder(label="right")
        self._imu = _ImuRecorder()
        self._rebind_output_paths()

        self._device: Any | None = None
        self._pipeline: Any | None = None
        self._q_preview: Any | None = None
        self._queues_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._imu_thread: threading.Thread | None = None
        # Host-side preview decoder (only used when rgb_preview_from_h264): a
        # dedicated thread decodes the RGB H.264 so the capture thread never pays
        # decode cost. While IDLE it feeds a persistent decoder every packet for
        # a smooth ~15 fps framing view; while RECORDING it decodes only the
        # newest keyframe (~6 fps) to keep CPU/heat off the capture window.
        self._preview_decode_thread: threading.Thread | None = None
        self._preview_packets: deque[bytes] = deque(maxlen=90)
        self._preview_lock = threading.Lock()
        self._preview_wake = threading.Event()
        self._stop_event = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_frame: Any = None
        self._substream_preview_queues: dict[str, Any] = {}
        self._substream_frames: dict[str, Any] = {}
        self._substream_callbacks: list[Callable[[Any], None]] = []
        self._imu_live_seq = 0
        self._calibration: dict[str, Any] | None = None
        self._built_camera_configs: list[tuple[str, str, Any, tuple[int, int], float]] = []
        self._recorded_artifacts: tuple[OakArtifact, ...] = ()
        self._health_lock = threading.Lock()
        self._recording_path_timestamps_ns: deque[int] = deque(maxlen=1200)
        self._recording = False
        self._capture_error: str | None = None
        # Mirror of the RGB recorder's frame count: preview health monitoring
        # reads ``stream._frame_count`` directly (see preview._sdk_frame_count).
        self._frame_count = 0

    def _rebind_output_paths(self) -> None:
        """Re-derive every recorder file path from the current ``_output_dir``.

        The SDK orchestrator rotates ``_output_dir`` for each episode (a 2nd+
        recording in one connect session) and rebinds only ``stream._output_dir``
        via ``_rebind_stream_output_dirs`` — it cannot reach the paths cached
        inside the per-camera recorders. Calling this at every
        ``start_recording`` makes consecutive recordings idempotently follow
        the current episode dir instead of clobbering the first one.
        """
        d = self._output_dir
        sid = self.id
        self._rgb.h264_path = d / f"{sid}.h264"
        self._rgb.mp4_path = d / f"{sid}.mp4"
        self._left.h264_path = d / f"{sid}.left.h264"
        self._left.mp4_path = d / f"{sid}.left.mp4"
        self._left.timestamps_path = d / f"{sid}.left.timestamps.jsonl"
        self._right.h264_path = d / f"{sid}.right.h264"
        self._right.mp4_path = d / f"{sid}.right.mp4"
        self._right.timestamps_path = d / f"{sid}.right.timestamps.jsonl"
        self._imu.path = d / f"{sid}.imu.jsonl"

    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> list:
        """Enumerate currently attached OAK devices for SDK-side discovery.

        Uses ``depthai.Device.getAllAvailableDevices`` (near-instant; ``timeout``
        is accepted for interface consistency). Each result pins its
        ``device_id`` to the OAK serial so auto-added streams bind to a specific
        board when several are attached. Never propagates errors.
        """
        from syncfield.discovery import DiscoveredDevice

        try:
            devices_info = dai.Device.getAllAvailableDevices()
        except Exception:  # noqa: BLE001
            return []

        results = []
        for info in devices_info:
            device_id = getattr(info, "deviceId", None) or ""
            name = getattr(info, "name", None) or "OAK"
            state = getattr(info, "state", None)
            state_str = (getattr(state, "name", None) or str(state)) if state else ""
            parts = [f"OAK · {device_id[:8]}…"] if device_id else ["OAK"]
            if state_str:
                parts.append(state_str.lower())
            results.append(
                DiscoveredDevice(
                    adapter_type="oak_camera",
                    adapter_cls=cls,
                    kind="video",
                    display_name=name or "OAK camera",
                    description=" · ".join(parts),
                    device_id=device_id or name or "oak",
                    construct_kwargs=({"device_id": device_id} if device_id else {}),
                    accepts_output_dir=True,
                )
            )
        return results

    def prepare(self) -> None:
        pass

    def connect(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            if _has_legacy_xlink_out():
                self._connect_legacy_pipeline()
            else:
                self._connect_v3_pipeline()
            self._install_depthai_bridge()
            self._recording = False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._capture_loop,
                name=f"desktop-oak-{self.id}",
                daemon=True,
            )
            self._thread.start()
            # The IMU (~200 Hz) gets its own thread so heavy video draining +
            # preview JPEG encoding on the capture thread can't starve it — a
            # shared thread dropped ~5% of IMU samples and pulled the rate to
            # ~180 Hz under full GUI load (measured).
            if self._imu.queue is not None:
                self._imu_thread = threading.Thread(
                    target=self._imu_loop,
                    name=f"desktop-oak-imu-{self.id}",
                    daemon=True,
                )
                self._imu_thread.start()
            if self._rgb_preview_from_h264:
                self._preview_wake.clear()
                self._preview_decode_thread = threading.Thread(
                    target=self._preview_decode_loop,
                    name=f"desktop-oak-preview-{self.id}",
                    daemon=True,
                )
                self._preview_decode_thread.start()
        except Exception:
            self.disconnect()
            raise

    def start_recording(self, session_clock: SessionClock) -> None:
        self._begin_recording_window(session_clock)
        if self._thread is None or not self._thread.is_alive():
            self.connect()
        # Follow the current episode dir: the orchestrator rotates _output_dir
        # per recording but can't rebind the nested recorder paths.
        self._rebind_output_paths()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        with self._health_lock:
            self._recording_path_timestamps_ns.clear()
        self._capture_error = None
        self._frame_count = 0
        for recorder in (self._rgb, self._left, self._right):
            recorder.begin_recording()
        self._imu.begin_recording()
        self._write_calibration_file()
        self._drain_stale_packets()
        self._recording = True

    def _write_calibration_file(self) -> None:
        if self._calibration is None:
            return
        path = self._output_dir / f"{self.id}.calibration.json"
        try:
            path.write_text(json.dumps(self._calibration, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("OAK %s: failed to write calibration file: %s", self.id, exc)

    def stop_recording(self) -> FinalizationReport:
        self._recording = False
        for recorder in (self._left, self._right):
            recorder.close_files()
        self._imu.close_file()

        # Primary (RGB) result decides completed-vs-failed, exactly as before
        # aux streams existed.
        self._rgb.close_files()
        mp4_available = False
        error = self._capture_error
        status = "completed"
        if self._rgb.frame_count > 0 and self._rgb.h264_path.exists():
            try:
                remux_h264_to_mp4(self._rgb.h264_path, self._rgb.mp4_path, fps=float(self._rgb_fps))
                mp4_available = (
                    self._rgb.mp4_path.exists() and self._rgb.mp4_path.stat().st_size > 0
                )
                if mp4_available:
                    try:
                        self._rgb.h264_path.unlink()
                    except OSError:
                        pass
            except Exception as exc:  # noqa: BLE001
                status = "failed"
                error = f"H.264 remux failed: {exc}"
                try:
                    if self._rgb.mp4_path.exists() and self._rgb.mp4_path.stat().st_size == 0:
                        self._rgb.mp4_path.unlink()
                except OSError:
                    pass
        elif error is None:
            status = "failed"
            error = "No decodable H.264 keyframe arrived during recording."

        if error is not None:
            status = "failed"

        aux_errors: list[str] = []
        artifacts: list[OakArtifact] = []
        for recorder in (self._left, self._right):
            if recorder.queue is None:
                continue
            aux_error = recorder.finish_recording(float(self._mono_fps))
            if aux_error is not None:
                aux_errors.append(aux_error)
            else:
                artifacts.append(
                    OakArtifact(
                        stream_id=f"{self.id}.{recorder.label}",
                        kind="video",
                        path=recorder.mp4_path,
                        frame_count=recorder.frame_count,
                    )
                )
        if self._imu.queue is not None:
            if self._imu.sample_count == 0:
                aux_errors.append("imu: no samples arrived during recording.")
            elif self._imu.path.exists():
                artifacts.append(
                    OakArtifact(
                        stream_id=f"{self.id}.imu",
                        kind="sensor",
                        path=self._imu.path,
                        frame_count=self._imu.sample_count,
                    )
                )
        self._recorded_artifacts = tuple(artifacts)
        if aux_errors:
            logger.warning("OAK %s aux capture losses: %s", self.id, "; ".join(aux_errors))
            if status == "completed":
                status = "partial"
                error = "; ".join(aux_errors)

        jitter_p95, jitter_p99 = compute_jitter_percentiles(self._rgb.intervals_ns)
        return FinalizationReport(
            stream_id=self.id,
            status=status,
            frame_count=self._rgb.frame_count,
            file_path=self._rgb.mp4_path if mp4_available else None,
            first_sample_at_ns=self._rgb.first_at,
            last_sample_at_ns=self._rgb.last_at,
            health_events=[],
            error=error,
            jitter_p95_ns=jitter_p95,
            jitter_p99_ns=jitter_p99,
            recording_anchor=self._recording_anchor(),
        )

    def _install_depthai_bridge(self) -> None:
        """Route depthai's native log records to HealthEvents. Idempotent."""
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
        self._stop_event.set()
        self._preview_wake.set()  # unblock the preview decode thread's wait()
        self._uninstall_depthai_bridge()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._imu_thread is not None:
            self._imu_thread.join(timeout=3.0)
            self._imu_thread = None
        if self._preview_decode_thread is not None:
            self._preview_decode_thread.join(timeout=3.0)
            self._preview_decode_thread = None
        for recorder in (self._rgb, self._left, self._right):
            recorder.close_files()
        self._imu.close_file()
        pipeline = getattr(self, "_pipeline", None)
        if pipeline is not None:
            stop = getattr(pipeline, "stop", None)
            if callable(stop):
                try:
                    stop()
                    time.sleep(0.3)
                except Exception:
                    pass
            self._pipeline = None
        if self._device is not None:
            close = getattr(self._device, "close", None)
            if callable(close):
                close()
            self._device = None
        with self._queues_lock:
            self._rgb.queue = None
            self._left.queue = None
            self._right.queue = None
            self._imu.queue = None
        self._q_preview = None
        self._substream_preview_queues = {}
        with self._frame_lock:
            self._substream_frames = {}

    def start(self, session_clock: SessionClock) -> None:
        self.connect()
        self.start_recording(session_clock)

    def stop(self) -> FinalizationReport:
        report = self.stop_recording()
        self.disconnect()
        return report

    @property
    def latest_frame(self) -> Any:
        with self._frame_lock:
            return self._latest_frame

    def latest_frame_for(self, substream_id: str) -> Any:
        """Latest preview frame for a video substream (``{id}.left``/``.right``)."""
        with self._frame_lock:
            return self._substream_frames.get(substream_id)

    def substream_capture_hz(self, substream_id: str) -> float:
        """True sensor capture rate of a video substream (30 fps), not the
        throttled ~10 fps preview cadence the UI would otherwise infer."""
        recorder = {
            f"{self.id}.left": self._left,
            f"{self.id}.right": self._right,
        }.get(substream_id)
        return recorder.capture_hz() if recorder is not None else 0.0

    def substreams(self) -> tuple[OakSubstream, ...]:
        """Derived streams currently live on this device's pipeline."""
        subs: list[OakSubstream] = []
        if self._left.queue is not None:
            subs.append(OakSubstream(f"{self.id}.left", "video", "Left mono", expected_hz=10.0))
        if self._right.queue is not None:
            subs.append(OakSubstream(f"{self.id}.right", "video", "Right mono", expected_hz=10.0))
        if self._imu.queue is not None:
            # expected_hz stays None: BNO086 grants roughly half the requested
            # rate (~195 Hz for a 400 Hz ask), so a threshold against the
            # request would misreport healthy capture as degraded.
            subs.append(OakSubstream(f"{self.id}.imu", "sensor", "IMU"))
        return tuple(subs)

    def recorded_artifacts(self) -> tuple[OakArtifact, ...]:
        """Aux files (left/right mono, IMU) written by the last recording.

        The desktop backend folds these into ``manifest.json`` after
        ``stop()`` so every OAK stream — not just the primary RGB the SDK
        orchestrator knows about — is discovered and aligned by the sync
        pipeline. Empty until a recording finalizes; excludes aux streams
        that produced no decodable output.
        """
        return self._recorded_artifacts

    def on_substream_sample(self, callback: Callable[[Any], None]) -> None:
        """Register a callback for derived-substream samples (IMU live data).

        Deliberately separate from ``on_sample`` — orchestrator sample writers
        must never see substream events, or IMU rows would pollute the
        composite's ``{id}.timestamps.jsonl``.
        """
        self._substream_callbacks.append(callback)

    def _emit_substream_sample(self, event: Any) -> None:
        for callback in self._substream_callbacks:
            try:
                callback(event)
            except Exception:  # noqa: BLE001
                logger.debug("substream sample callback failed for %s", self.id)

    @property
    def device_key(self) -> str | None:
        return f"oak_camera:{self._device_id}" if self._device_id else None

    def _connect_legacy_pipeline(self) -> None:
        device_info = dai.DeviceInfo(self._device_id) if self._device_id else None
        pipeline = self._build_legacy_pipeline()
        device = (
            dai.Device(pipeline, device_info) if device_info is not None else dai.Device(pipeline)
        )
        self._device = device
        self._rgb.queue = device.getOutputQueue(name="h264", maxSize=30, blocking=False)
        self._q_preview = device.getOutputQueue(name="preview", maxSize=4, blocking=False)
        logger.info(
            "OAK %s: legacy DepthAI (v2) API detected; mono/IMU capture requires v3 — "
            "recording RGB only",
            self.id,
        )

    def _connect_v3_pipeline(self) -> None:
        device_info = dai.DeviceInfo(self._device_id) if self._device_id else None
        self._device = dai.Device(device_info) if device_info is not None else dai.Device()
        pipeline = self._build_v3_pipeline(self._device)
        self._calibration = _build_calibration_summary(self._device, self._built_camera_configs)
        if self._calibration is None:
            logger.warning(
                "OAK %s: device calibration unavailable; sessions will lack %s.calibration.json",
                self.id,
                self.id,
            )
        pipeline.start()
        self._pipeline = pipeline

    def _build_legacy_pipeline(self) -> Any:
        pipeline = dai.Pipeline()
        cam = pipeline.create(dai.node.ColorCamera)
        cam.setBoardSocket(dai.CameraBoardSocket.CAM_A)
        cam.setResolution(dai.ColorCameraProperties.SensorResolution.THE_720_P)
        cam.setFps(float(self._rgb_fps))
        cam.setPreviewSize(*self._preview_resolution)
        cam.setInterleaved(False)

        enc = pipeline.create(dai.node.VideoEncoder)
        enc.setDefaultProfilePreset(
            self._rgb_fps,
            dai.VideoEncoderProperties.Profile.H264_MAIN,
        )
        _configure_h264_encoder_for_segmented_recording(enc, self._rgb_fps)
        if self._h264_bitrate_bps > 0:
            enc.setBitrate(self._h264_bitrate_bps)
        cam.video.link(enc.input)

        h264_out = _create_xlink_out(pipeline)
        h264_out.setStreamName("h264")
        enc.bitstream.link(h264_out.input)

        preview_out = _create_xlink_out(pipeline)
        preview_out.setStreamName("preview")
        cam.preview.link(preview_out.input)
        return pipeline

    def _build_v3_pipeline(self, device: Any) -> Any:
        pipeline = dai.Pipeline(device)
        socket_names = _connected_socket_names(device)
        imu_name = _connected_imu_name(device)
        self._built_camera_configs = []
        # Hardware frame sync: with the stereo pair present, CAM_A drives the
        # FSYNC line (OUTPUT) and the monos follow it (INPUT), so all three
        # sensors expose the shutter at the same instant. Measured: rgb<->mono
        # per-frame device-timestamp residual drops from ~4.6 ms (free-run) to
        # ~0.02 ms. Only meaningful when the monos are built to follow.
        stereo_available = (
            self._enable_mono and socket_names is not None and {"CAM_B", "CAM_C"} <= socket_names
        )

        cam = pipeline.create(dai.node.Camera)
        try:
            cam.build(dai.CameraBoardSocket.CAM_A)
        except TypeError:
            cam.build()
        if stereo_available:
            _apply_frame_sync(cam, "OUTPUT", self.id, "CAM_A")

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
        )
        _configure_h264_encoder_for_segmented_recording(encoder, self._rgb_fps)
        if self._h264_bitrate_bps > 0:
            encoder.setBitrateKbps(max(1, self._h264_bitrate_bps // 1000))
        self._rgb.queue = encoder.out.createOutputQueue()
        self._built_camera_configs.append(
            (
                "rgb",
                "CAM_A",
                dai.CameraBoardSocket.CAM_A,
                self._rgb_resolution,
                float(self._rgb_fps),
            )
        )

        # Skip the RGB ISP preview output when previewing from H.264: on an
        # ISP-bound config it is a second output on the busiest camera (CAM_A),
        # which starves the high-res encode stream. The preview is decoded from
        # the encode bitstream instead (see _preview_decode_loop).
        if not self._rgb_preview_from_h264:
            preview_out = cam.requestOutput(
                self._preview_resolution,
                dai.ImgFrame.Type.BGR888p,
                fps=min(10.0, float(self._rgb_fps)),
            )
            self._q_preview = preview_out.createOutputQueue()

        if self._enable_mono:
            if stereo_available:
                for socket, socket_name, recorder in (
                    (dai.CameraBoardSocket.CAM_B, "CAM_B", self._left),
                    (dai.CameraBoardSocket.CAM_C, "CAM_C", self._right),
                ):
                    mono = pipeline.create(dai.node.Camera)
                    mono.build(socket)
                    _apply_frame_sync(mono, "INPUT", self.id, socket_name)
                    mono_out = mono.requestOutput(
                        self._mono_resolution,
                        dai.ImgFrame.Type.NV12,
                        fps=float(self._mono_fps),
                    )
                    mono_encoder = pipeline.create(dai.node.VideoEncoder)
                    mono_encoder.build(
                        input=mono_out,
                        frameRate=float(self._mono_fps),
                        profile=dai.VideoEncoderProperties.Profile.H264_MAIN,
                    )
                    _configure_h264_encoder_for_segmented_recording(mono_encoder, self._mono_fps)
                    if self._mono_bitrate_bps > 0:
                        mono_encoder.setBitrateKbps(max(1, self._mono_bitrate_bps // 1000))
                    recorder.queue = mono_encoder.out.createOutputQueue(maxSize=30, blocking=False)
                    self._built_camera_configs.append(
                        (
                            recorder.label,
                            socket_name,
                            socket,
                            self._mono_resolution,
                            float(self._mono_fps),
                        )
                    )
                    if self._enable_mono_preview:
                        mono_preview = mono.requestOutput(
                            self._preview_resolution,
                            dai.ImgFrame.Type.BGR888p,
                            fps=min(10.0, float(self._mono_fps)),
                        )
                        self._substream_preview_queues[f"{self.id}.{recorder.label}"] = (
                            mono_preview.createOutputQueue(maxSize=4, blocking=False)
                        )
            else:
                logger.warning(
                    "OAK %s: stereo sockets unavailable (connected=%s); recording RGB only",
                    self.id,
                    sorted(socket_names) if socket_names else socket_names,
                )

        if self._enable_imu:
            if imu_name:
                imu = pipeline.create(dai.node.IMU)
                # One combined enable call on purpose: enabling the sensors
                # separately degrades BNO086 synced output to ~52 Hz, while
                # the combined form delivers ~195 Hz (measured on hardware).
                imu.enableIMUSensor(
                    [
                        dai.IMUSensor.ACCELEROMETER_RAW,
                        dai.IMUSensor.GYROSCOPE_RAW,
                        dai.IMUSensor.ROTATION_VECTOR,
                    ],
                    self._imu_rate_hz,
                )
                imu.setBatchReportThreshold(5)
                imu.setMaxBatchReports(30)
                # Large host queue: the IMU (~196 Hz) shares one capture thread
                # with three H.264 video drains, so a busy loop iteration must
                # not overflow it. 200 gives ~1 s of device-side buffering.
                self._imu.queue = imu.out.createOutputQueue(maxSize=200, blocking=False)
            else:
                logger.info("OAK %s: no IMU reported by device; skipping IMU capture", self.id)

        return pipeline

    def _capture_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._drain_preview()
                got_packets = False
                for recorder in (self._rgb, self._left, self._right):
                    packets = self._drain_queue(recorder)
                    for pkt in packets:
                        self._handle_camera_packet(recorder, pkt)
                    got_packets = got_packets or bool(packets)
                if not got_packets:
                    self._stop_event.wait(0.002)
        except Exception as exc:  # noqa: BLE001
            self._capture_error = f"{type(exc).__name__}: {exc}"
            logger.exception("OAK capture loop failed for %s", self.id)

    def _imu_loop(self) -> None:
        """Drain the IMU on its own thread, decoupled from video/preview load."""
        try:
            while not self._stop_event.is_set():
                messages = self._drain_queue(self._imu)
                for msg in messages:
                    self._handle_imu_message(msg)
                if not messages:
                    self._stop_event.wait(0.001)
        except Exception as exc:  # noqa: BLE001
            self._capture_error = f"{type(exc).__name__}: {exc}"
            logger.exception("OAK IMU loop failed for %s", self.id)

    def _drain_preview(self) -> None:
        if self._q_preview is not None:
            msg = self._q_preview.tryGet()
            if msg is not None:
                frame = msg.getCvFrame()
                with self._frame_lock:
                    self._latest_frame = frame
        for substream_id, queue in self._substream_preview_queues.items():
            msg = queue.tryGet()
            if msg is None:
                continue
            frame = msg.getCvFrame()
            with self._frame_lock:
                self._substream_frames[substream_id] = frame

    def _drain_queue(self, holder: _H264Recorder | _ImuRecorder) -> tuple[Any, ...]:
        with self._queues_lock:
            queue = holder.queue
            if queue is None:
                return ()
            get_all = getattr(queue, "tryGetAll", None)
            if callable(get_all):
                packets = tuple(get_all())
                if packets:
                    return packets
            pkt = queue.tryGet()
            return (pkt,) if pkt is not None else ()

    def _drain_stale_packets(self) -> None:
        for recorder in (self._rgb, self._left, self._right):
            for pkt in self._drain_queue(recorder):
                try:
                    recorder.remember_parameter_sets(_packet_bytes(pkt))
                except Exception:  # noqa: BLE001
                    logger.debug("stale OAK H.264 packet drain failed for %s", self.id)
        # Pre-recording IMU samples are preview-phase noise — discard them so
        # the JSONL starts at the recording window like the video files do.
        self._drain_queue(self._imu)

    def _handle_camera_packet(self, recorder: _H264Recorder, pkt: Any) -> None:
        capture_ns = time.monotonic_ns()
        data = _packet_bytes(pkt)
        recorder.remember_parameter_sets(data)
        recorder.observe_capture(capture_ns)
        if recorder.is_primary:
            self._observe_recording_path_frame(capture_ns)
            if self._rgb_preview_from_h264:
                self._feed_preview_packet(data)
        if not self._recording:
            return

        device_ts_ns = _timestamp_ns(getattr(pkt, "getTimestampDevice", None))
        if not recorder.write_frame(data, capture_ns, device_ts_ns):
            return
        if recorder.is_primary:
            self._observe_first_frame(capture_ns, device_ts_ns)
            self._frame_count = recorder.frame_count
            self._emit_sample(
                _sample_event(
                    stream_id=self.id,
                    frame_number=recorder.frame_count - 1,
                    capture_ns=capture_ns,
                    device_ts_ns=device_ts_ns,
                )
            )

    def _handle_imu_message(self, msg: Any) -> None:
        capture_ns = time.monotonic_ns()
        recording = self._recording
        for pkt in getattr(msg, "packets", ()) or ():
            reading = _imu_reading(pkt)
            if reading is None:
                continue
            channels, device_ns = reading
            if recording:
                # Canonical SensorSample schema so the sync pipeline's
                # load_sensor_jsonl reads it (needs capture_ns + a channels
                # dict). clock_domain is omitted so sync inherits the primary
                # stream's domain, keeping the intra-host alignment path.
                # write_row is a thread-safe no-op once the file is closed.
                self._imu.write_row(
                    {
                        "frame_number": self._imu.sample_count,
                        "capture_ns": capture_ns,
                        "channels": channels,
                        "clock_source": "host_monotonic",
                        "uncertainty_ns": 5_000_000,
                        "device_timestamp_ns": device_ns,
                    }
                )
            if self._substream_callbacks:
                self._emit_substream_sample(
                    _imu_live_event(f"{self.id}.imu", channels, self._imu_live_seq, capture_ns)
                )
            self._imu_live_seq += 1

    def recording_health_window(self, start_ns: int, end_ns: int) -> tuple[int, ...]:
        """Return encoded-frame dequeue timestamps for a preflight window."""
        with self._health_lock:
            samples = tuple(self._recording_path_timestamps_ns)
        return tuple(ts for ts in samples if start_ns <= ts <= end_ns)

    def _observe_recording_path_frame(self, capture_ns: int) -> None:
        with self._health_lock:
            self._recording_path_timestamps_ns.append(capture_ns)

    def _feed_preview_packet(self, data: bytes) -> None:
        """Hand one primary RGB H.264 packet to the decode thread (H.264 preview
        mode only). Cheap: bounded append under a lock + wake — the decode itself
        runs on ``_preview_decode_loop`` so the capture drain never pays decode
        cost. The deque is capped, so if the decoder falls behind it drops the
        oldest packets and resyncs at the next keyframe rather than growing.
        """
        with self._preview_lock:
            self._preview_packets.append(data)
        self._preview_wake.set()

    def _preview_decode_loop(self) -> None:
        """Decode the RGB H.264 into preview frames off the capture thread.

        Feeds every packet to a PERSISTENT decoder — a fresh context per keyframe
        does not reliably emit a frame from a single access unit (the decoder is
        stream-oriented), which showed on-device as a permanent "NO SIGNAL". The
        decode runs on this dedicated thread (libav releases the GIL) and never
        touches the capture path. Store cadence is capped so we don't churn the
        BGR reformat/ndarray faster than the UI serves. Best-effort throughout: a
        decode hiccup or missing PyAV leaves the last frame and never raises.
        """
        codec: Any = None
        last_store_ns = 0
        while not self._stop_event.is_set():
            if not self._preview_wake.wait(timeout=0.5):
                continue
            self._preview_wake.clear()
            if self._stop_event.is_set():
                break
            with self._preview_lock:
                packets = tuple(self._preview_packets)
                self._preview_packets.clear()
            if not packets:
                continue
            if codec is None:
                codec = self._new_h264_decoder()
            frame = self._decode_stream(codec, packets)
            if frame is None:
                continue
            now = time.monotonic_ns()
            if now - last_store_ns < self._PREVIEW_STORE_INTERVAL_NS:
                continue
            last_store_ns = now
            with self._frame_lock:
                self._latest_frame = frame

    #: Cap _latest_frame updates to ~12 fps — the UI serves no faster, so churning
    #: the reformat/ndarray at the full 30 fps decode rate is wasted work.
    _PREVIEW_STORE_INTERVAL_NS = 80_000_000

    @staticmethod
    def _new_h264_decoder() -> Any:
        """A fresh persistent H.264 decode context, or None if PyAV is absent."""
        try:
            import av  # type: ignore[import-not-found]

            return av.CodecContext.create("h264", "r")
        except Exception:  # noqa: BLE001
            return None

    def _decode_stream(self, codec: Any, packets: tuple[bytes, ...]) -> Any:
        """Feed *packets* in order to the persistent *codec*, returning the newest
        decoded BGR frame (preview-sized) or None. Best-effort: a P-frame before
        the decoder has seen a keyframe just yields nothing until the next IDR —
        never raises into the capture path.
        """
        if codec is None:
            return None
        latest = None
        width, height = self._preview_resolution
        try:
            for data in packets:
                for packet in codec.parse(data):
                    for frame in codec.decode(packet):
                        latest = frame
        except Exception:  # noqa: BLE001
            logger.debug("OAK %s: preview stream decode hiccup", self.id)
            return None
        if latest is None:
            return None
        try:
            return latest.reformat(width=width, height=height, format="bgr24").to_ndarray()
        except Exception:  # noqa: BLE001
            return None


def _apply_frame_sync(cam: Any, role: str, stream_id: str, socket_name: str) -> None:
    """Put one camera into hardware frame-sync as OUTPUT (master) or INPUT.

    Best-effort + feature-detected: a DepthAI build or board that lacks
    ``setFrameSyncMode`` / the requested role just falls back to free-running
    capture (the sync pipeline still aligns via device timestamps, only less
    tightly), so a failure is logged, never fatal.
    """
    try:
        control = cam.initialControl
        mode = getattr(dai.CameraControl.FrameSyncMode, role)
        control.setFrameSyncMode(mode)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "OAK %s: hardware frame sync unavailable for %s (%s); using free-run",
            stream_id,
            socket_name,
            exc,
        )


def _connected_socket_names(device: Any) -> frozenset[str] | None:
    """Names of camera sockets the device reports, or None when unknowable.

    ``None`` (probe failed) deliberately reads as "don't build mono nodes":
    building a pipeline against absent sockets fails the whole connect, which
    would regress RGB capture on boards in a degraded state.
    """
    try:
        features = device.getConnectedCameraFeatures()
    except Exception:  # noqa: BLE001
        return None
    names: set[str] = set()
    for feature in features or ():
        socket = getattr(feature, "socket", None)
        if socket is None:
            continue
        names.add(str(getattr(socket, "name", socket)).split(".")[-1])
    return frozenset(names)


def _connected_imu_name(device: Any) -> str:
    try:
        getter = getattr(device, "getConnectedIMU", None)
        value = getter() if callable(getter) else ""
    except Exception:  # noqa: BLE001
        return ""
    name = str(value or "").strip()
    return "" if name.upper() == "NONE" else name


def _imu_reading(pkt: Any) -> tuple[dict[str, float], int | None] | None:
    """Extract canonical scalar channels + device timestamp from an IMU packet.

    Channels are raw and lossless: ``accel_x/y/z`` (m/s²), ``gyro_x/y/z``
    (rad/s), ``quat_x/y/z/w`` (rotation-vector quaternion). Returns ``None``
    when the packet carries none of the three sensors.
    """
    accel = getattr(pkt, "acceleroMeter", None)
    gyro = getattr(pkt, "gyroscope", None)
    rotation = getattr(pkt, "rotationVector", None)

    device_ns: int | None = None
    for report in (accel, gyro, rotation):
        if report is None:
            continue
        device_ns = _timestamp_ns(getattr(report, "getTimestampDevice", None))
        if device_ns is not None:
            break

    channels: dict[str, float] = {}
    if accel is not None and hasattr(accel, "x"):
        channels["accel_x"] = accel.x
        channels["accel_y"] = accel.y
        channels["accel_z"] = accel.z
    if gyro is not None and hasattr(gyro, "x"):
        channels["gyro_x"] = gyro.x
        channels["gyro_y"] = gyro.y
        channels["gyro_z"] = gyro.z
    if rotation is not None and hasattr(rotation, "i"):
        channels["quat_x"] = rotation.i
        channels["quat_y"] = rotation.j
        channels["quat_z"] = rotation.k
        channels["quat_w"] = rotation.real
    return (channels, device_ns) if channels else None


def _quat_to_euler_deg(i: float, j: float, k: float, real: float) -> tuple[float, float, float]:
    """Quaternion (x, y, z, w) → intrinsic roll/pitch/yaw in degrees."""
    x, y, z, w = i, j, k, real
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2.0 * (w * y - z * x))))
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)


def _imu_live_event(stream_id: str, channels: dict[str, float], seq: int, capture_ns: int) -> Any:
    """Shape one IMU reading as a live substream sample for the preview UI.

    Carries the raw scalar channels for the sensor chart plus roll/pitch/yaw
    (derived from the rotation vector) and the quaternion as a vector channel,
    both of which drive the orientation pose panel.
    """
    enriched: dict[str, Any] = dict(channels)
    if all(k in channels for k in ("quat_x", "quat_y", "quat_z", "quat_w")):
        quat = [channels["quat_x"], channels["quat_y"], channels["quat_z"], channels["quat_w"]]
        roll, pitch, yaw = _quat_to_euler_deg(*quat)
        enriched["roll"] = roll
        enriched["pitch"] = pitch
        enriched["yaw"] = yaw
        enriched["quat"] = quat
    return SimpleNamespace(
        stream_id=stream_id,
        frame_number=seq,
        capture_ns=capture_ns,
        channels=enriched,
    )


def _build_calibration_summary(
    device: Any,
    camera_configs: list[tuple[str, str, Any, tuple[int, int], float]],
) -> dict[str, Any] | None:
    """Serialize the device's EEPROM calibration for the recorded streams.

    ``camera_configs`` rows are ``(label, socket_name, socket, (w, h), fps)``
    for every camera actually built into the pipeline. Intrinsics are scaled
    to each recorded resolution so downstream consumers can use them without
    knowing the sensor-native calibration size. Every section is best-effort:
    a missing piece is omitted, never fatal; returns None only when the
    calibration handler itself is unreadable.
    """
    try:
        calibration = device.readCalibration()
    except Exception as exc:  # noqa: BLE001
        logger.warning("OAK calibration read failed: %s", exc)
        return None

    summary: dict[str, Any] = {"schema": "syncfield.oak_calibration.v1"}

    device_section: dict[str, Any] = {}
    for key, getter_name in (
        ("device_id", "getDeviceId"),
        ("name", "getDeviceName"),
        ("product", "getProductName"),
    ):
        try:
            getter = getattr(device, getter_name, None)
            if callable(getter):
                device_section[key] = str(getter())
        except Exception:  # noqa: BLE001
            pass
    summary["device"] = device_section

    try:
        summary["eeprom"] = calibration.eepromToJson()
    except Exception:  # noqa: BLE001
        pass

    streams: dict[str, Any] = {}
    for label, socket_name, socket, resolution, fps in camera_configs:
        stream_section: dict[str, Any] = {
            "socket": socket_name,
            "resolution": list(resolution),
            "fps": fps,
        }
        try:
            stream_section["intrinsics"] = calibration.getCameraIntrinsics(
                socket, resolution[0], resolution[1]
            )
        except Exception:  # noqa: BLE001
            try:
                stream_section["intrinsics_native"] = calibration.getCameraIntrinsics(socket)
            except Exception:  # noqa: BLE001
                pass
        try:
            stream_section["distortion_coefficients"] = list(
                calibration.getDistortionCoefficients(socket)
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            stream_section["hfov_deg"] = float(calibration.getFov(socket))
        except Exception:  # noqa: BLE001
            pass
        streams[label] = stream_section
    summary["streams"] = streams

    sockets_by_label = {label: socket for label, _, socket, _, _ in camera_configs}
    extrinsics: dict[str, Any] = {}
    for name, src_label, dst_label in (
        ("left_to_right", "left", "right"),
        ("rgb_to_left", "rgb", "left"),
        ("rgb_to_right", "rgb", "right"),
    ):
        src = sockets_by_label.get(src_label)
        dst = sockets_by_label.get(dst_label)
        if src is None or dst is None:
            continue
        try:
            extrinsics[name] = _transform_section(calibration.getCameraExtrinsics(src, dst))
        except Exception:  # noqa: BLE001
            pass
    if extrinsics:
        summary["extrinsics"] = extrinsics

    stereo: dict[str, Any] = {}
    if "left" in sockets_by_label and "right" in sockets_by_label:
        try:
            stereo["baseline_cm"] = float(calibration.getBaselineDistance())
        except Exception:  # noqa: BLE001
            pass
        stereo["left_socket"] = next(
            (name for label, name, _, _, _ in camera_configs if label == "left"), None
        )
        stereo["right_socket"] = next(
            (name for label, name, _, _, _ in camera_configs if label == "right"), None
        )
    if stereo:
        summary["stereo"] = stereo

    imu_section: dict[str, Any] = {}
    try:
        imu_name = _connected_imu_name(device)
        if imu_name:
            imu_section["type"] = imu_name
    except Exception:  # noqa: BLE001
        pass
    imu_target = sockets_by_label.get("left") or sockets_by_label.get("rgb")
    imu_target_label = "left" if "left" in sockets_by_label else "rgb"
    if imu_target is not None:
        try:
            imu_section[f"imu_to_{imu_target_label}"] = _transform_section(
                calibration.getImuToCameraExtrinsics(imu_target)
            )
        except Exception:  # noqa: BLE001
            pass
    if imu_section:
        summary["imu"] = imu_section

    return summary


def _transform_section(matrix_4x4: Any) -> dict[str, Any]:
    rows = [list(row) for row in matrix_4x4]
    return {
        "rotation": [row[:3] for row in rows[:3]],
        "translation_cm": [rows[0][3], rows[1][3], rows[2][3]],
    }


def _timestamp_ns(getter: Any) -> int | None:
    if not callable(getter):
        return None
    try:
        value = getter()
    except Exception:  # noqa: BLE001
        return None
    if value is None:
        return None
    total_seconds = getattr(value, "total_seconds", None)
    if callable(total_seconds):
        return int(cast(Callable[[], float], total_seconds)() * 1_000_000_000)
    return None


def _sample_event(
    *,
    stream_id: str,
    frame_number: int,
    capture_ns: int,
    device_ts_ns: int | None,
) -> SampleEvent:
    kwargs: dict[str, Any] = {
        "stream_id": stream_id,
        "frame_number": frame_number,
        "capture_ns": capture_ns,
    }
    if device_ts_ns is not None:
        if _SAMPLE_EVENT_SUPPORTS_DEVICE_NS:
            kwargs["device_ns"] = device_ts_ns
        else:
            # syncfield 0.3.x has no SampleEvent.device_ns; for video
            # streams the orchestrator serializes channels as top-level
            # FrameTimestamp extras, preserving device_timestamp_ns.
            kwargs["channels"] = {"device_timestamp_ns": device_ts_ns}
    return SampleEvent(**kwargs)


def _has_legacy_xlink_out() -> bool:
    return hasattr(dai.Pipeline, "createXLinkOut") or hasattr(dai.node, "XLinkOut")


def _create_xlink_out(pipeline: Any) -> Any:
    create_xlink_out = getattr(pipeline, "createXLinkOut", None)
    if callable(create_xlink_out):
        return create_xlink_out()
    return pipeline.create(dai.node.XLinkOut)


def _configure_h264_encoder_for_segmented_recording(encoder: Any, fps: int) -> None:
    """Tune encoder output so files can start cleanly after live preview."""
    set_num_b_frames = getattr(encoder, "setNumBFrames", None)
    if callable(set_num_b_frames):
        try:
            set_num_b_frames(0)
        except Exception:  # noqa: BLE001
            pass
    set_keyframe_frequency = getattr(encoder, "setKeyframeFrequency", None)
    if callable(set_keyframe_frequency):
        try:
            set_keyframe_frequency(max(1, min(int(fps), 5)))
        except Exception:  # noqa: BLE001
            pass


def _packet_bytes(pkt: Any) -> bytes:
    data = pkt.getData()
    tobytes = getattr(data, "tobytes", None)
    return tobytes() if callable(tobytes) else bytes(data)


def _contains_h264_parameter_sets(data: bytes) -> bool:
    nal_types = {nal_type for nal_type, _ in _iter_h264_annex_b_nalus(data)}
    return 7 in nal_types and 8 in nal_types


def _contains_h264_nal_type(data: bytes, nal_type: int) -> bool:
    return any(kind == nal_type for kind, _ in _iter_h264_annex_b_nalus(data))


def _iter_h264_annex_b_nalus(data: bytes) -> tuple[tuple[int, bytes], ...]:
    nalus: list[tuple[int, bytes]] = []
    starts = list(_annex_b_start_codes(data))
    for idx, (start, header) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(data)
        if header >= end:
            continue
        nal_type = data[header] & 0x1F
        nalus.append((nal_type, data[start:end]))
    return tuple(nalus)


def _annex_b_start_codes(data: bytes) -> tuple[tuple[int, int], ...]:
    starts: list[tuple[int, int]] = []
    i = 0
    size = len(data)
    while i + 3 <= size:
        if i + 4 <= size and data[i : i + 4] == b"\x00\x00\x00\x01":
            starts.append((i, i + 4))
            i += 4
            continue
        if data[i : i + 3] == b"\x00\x00\x01":
            starts.append((i, i + 3))
            i += 3
            continue
        i += 1
    return tuple(starts)
