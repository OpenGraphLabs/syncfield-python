"""Production OVISION stereo-inertial UVC capture adapter (Linux).

The camera emits one hardware-synchronised 3840x1080 H.264 stream containing
two 1920x1080 eyes side-by-side.  Per-frame YCTC SEI carries exposure timing,
500 Hz gyro/accel and 100 Hz magnetometer data.  Encoded video is muxed without
decode/re-encode; all sensor/timing data are also materialised as JSONL.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import av  # type: ignore[import-not-found]

from syncfield.adapters._video_encoder import compute_jitter_percentiles
from syncfield.adapters.ovision_calibration import OvisionCalibration, read_ovision_calibration
from syncfield.adapters.ovision_controls import (
    OvisionCaptureProfile,
    configure_ovision_capture_profile,
)
from syncfield.adapters.ovision_metadata import (
    OvisionFrameMetadata,
    OvisionMetadataError,
    parse_ovision_h264_metadata,
)
from syncfield.adapters.uvc_mjpeg_passthrough import PassthroughWriter
from syncfield.clock import SessionClock
from syncfield.stream import StreamBase
from syncfield.types import FinalizationReport, HealthEvent, HealthEventKind, SampleEvent, StreamCapabilities

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OvisionArtifact:
    stream_id: str
    kind: str
    path: Path
    frame_count: int


@dataclass
class _SidecarSinks:
    directory: Path
    stem: str
    frame_meta: Any
    imu: Any
    accel: Any
    gyro: Any
    mag: Any
    frame_count: int = 0
    imu_count: int = 0
    accel_count: int = 0
    gyro_count: int = 0
    mag_count: int = 0

    @classmethod
    def open(cls, directory: Path, stem: str) -> "_SidecarSinks":
        directory.mkdir(parents=True, exist_ok=True)
        return cls(
            directory,
            stem,
            (directory / f"{stem}.stereo.jsonl").open("w", encoding="utf-8"),
            (directory / f"{stem}.imu.jsonl").open("w", encoding="utf-8"),
            (directory / f"{stem}.accel.jsonl").open("w", encoding="utf-8"),
            (directory / f"{stem}.gyro.jsonl").open("w", encoding="utf-8"),
            (directory / f"{stem}.mag.jsonl").open("w", encoding="utf-8"),
        )

    def close(self) -> None:
        for file in (self.frame_meta, self.imu, self.accel, self.gyro, self.mag):
            file.flush()
            file.close()


class OvisionCameraStream(StreamBase):
    """One physical OVISION camera as one composite stereo-inertial stream."""

    _discovery_kind = "video"
    _discovery_adapter_type = "ovision_camera"

    def __init__(
        self,
        id: str,
        output_dir: str | Path,
        *,
        video_device: str | Path = "/dev/video0",
        usb_serial: str | None = None,
        width: int = 3840,
        height: int = 1080,
        fps: float = 30.0,
        preview_interval_s: float = 0.5,
    ) -> None:
        super().__init__(
            id=id,
            kind="video",
            capabilities=StreamCapabilities(
                provides_audio_track=False,
                supports_precise_timestamps=True,
                is_removable=True,
                produces_file=True,
                target_hz=fps,
            ),
        )
        if (width, height) != (3840, 1080):
            raise ValueError("OVISION production mode is fixed at 3840x1080")
        self._output_dir = Path(output_dir)
        self._video_device = Path(video_device)
        self._usb_serial = usb_serial
        self._width = width
        self._height = height
        self._fps = float(fps)
        self._preview_interval_s = float(preview_interval_s)
        self._file_path = self._output_dir / f"{id}.mp4"
        self._input: Any = None
        self._writer: PassthroughWriter | None = None
        self._sinks: _SidecarSinks | None = None
        self._prepared: tuple[Path, PassthroughWriter, _SidecarSinks] | None = None
        self._calibration: OvisionCalibration | None = None
        self._capture_profile: OvisionCaptureProfile | None = None
        self._calibration_document: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None
        self._writer_thread: threading.Thread | None = None
        self._packet_queue: queue.Queue[tuple[Any, bytes, int, bool]] = queue.Queue(maxsize=3600)
        self._stop_event = threading.Event()
        # READY means more than "V4L2 opened": at least one live H.264 packet
        # must carry a valid manufacturer SEI payload with stereo timing and
        # IMU samples.  The kiosk waits on this latch before enabling record.
        self._ready_event = threading.Event()
        self._recording_lock = threading.Lock()
        self._rotation_condition = threading.Condition(self._recording_lock)
        self._sink_lock = threading.Lock()
        self._recording = False
        self._frame_count = 0
        self._first_at: int | None = None
        self._last_at: int | None = None
        self._prev_capture_ns: int | None = None
        self._intervals_ns: list[int] = []
        self._capture_error: str | None = None
        self._recorded_artifacts: tuple[OvisionArtifact, ...] = ()
        self._frame_lock = threading.Lock()
        self._latest_frame: Any = None
        self._last_preview_at = 0.0
        self._preview_wake = threading.Event()
        self._preview_packet: bytes | None = None
        self._preview_thread: threading.Thread | None = None
        self._started_on_keyframe = False
        self._rotation_requested = False
        self._rotation_keyframe: tuple[Any, bytes, int, bool] | None = None

    @property
    def output_name(self) -> str:
        return self.id

    @property
    def device_key(self) -> tuple[str, str]:
        return ("ovision_camera", self._usb_serial or str(self._video_device))

    @property
    def latest_frame(self) -> Any:
        with self._frame_lock:
            return self._latest_frame

    def capture_ready(self) -> bool:
        """Whether live video plus strict stereo-inertial metadata is valid."""
        return (
            self._ready_event.is_set()
            and self._thread is not None
            and self._thread.is_alive()
            and self._capture_error is None
        )

    def prepare(self) -> None:
        if self._input is not None:
            return
        calibration = read_ovision_calibration(self._video_device)
        if calibration.output_mode != "internal":
            raise RuntimeError(
                f"OVISION must use INTERNAL stereo FSYNC, got {calibration.output_mode}"
            )
        capture_profile = configure_ovision_capture_profile(self._video_device)
        logger.info(
            "OVISION production image profile: exposure=%dus gain=%.3fx bitrate=%dkbps",
            capture_profile.exposure_time_us,
            capture_profile.gain_multiplier,
            capture_profile.bitrate_kbps,
        )
        document = calibration.capture_document(usb_serial=self._usb_serial)
        document["capture_profile"] = capture_profile.capture_document()
        options = {
            "video_size": f"{self._width}x{self._height}",
            "framerate": str(int(round(self._fps))),
            "input_format": "h264",
            "fflags": "nobuffer+flush_packets",
            "flags": "low_delay",
            "analyzeduration": "0",
            "max_delay": "0",
        }
        self._input = av.open(str(self._video_device), format="v4l2", options=options)
        stream = self._input.streams.video[0]
        if (stream.codec_context.width, stream.codec_context.height) != (self._width, self._height):
            self._input.close()
            self._input = None
            raise RuntimeError("OVISION did not negotiate 3840x1080 H.264")
        self._calibration = calibration
        self._capture_profile = capture_profile
        self._calibration_document = document

    def connect(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._input is None:
            self.prepare()
        self._stop_event.clear()
        self._ready_event.clear()
        self._capture_error = None
        self._thread = threading.Thread(target=self._capture_loop, name=f"ovision-{self.id}", daemon=True)
        self._thread.start()
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name=f"ovision-writer-{self.id}", daemon=True
        )
        self._writer_thread.start()
        self._preview_thread = threading.Thread(
            target=self._preview_loop, name=f"ovision-preview-{self.id}", daemon=True
        )
        self._preview_thread.start()

    def _write_calibration(self, directory: Path) -> None:
        assert self._calibration is not None and self._calibration_document is not None
        (directory / f"{self.id}.calibration.json").write_text(
            json.dumps(self._calibration_document, indent=2) + "\n", encoding="utf-8"
        )
        (directory / f"{self.id}.calibration.yaml").write_text(
            self._calibration.yaml_text, encoding="utf-8"
        )
        (directory / f"{self.id}.calibration.bin").write_bytes(self._calibration.blob)

    def start_recording(self, session_clock: SessionClock) -> None:
        self._begin_recording_window(session_clock)
        if self._thread is None or not self._thread.is_alive():
            self.connect()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._write_calibration(self._output_dir)
        writer = PassthroughWriter.open(self._file_path, template_stream=self._input.streams.video[0])
        sinks = _SidecarSinks.open(self._output_dir, self.id)
        with self._sink_lock:
            self._frame_count = 0
            self._first_at = None
            self._last_at = None
            self._prev_capture_ns = None
            self._intervals_ns = []
            self._capture_error = None
            self._started_on_keyframe = False
            self._writer = writer
            self._sinks = sinks
        with self._recording_lock:
            self._recording = True

    def _capture_loop(self) -> None:
        packet_iter = None
        try:
            while not self._stop_event.is_set():
                if packet_iter is None:
                    packet_iter = self._input.demux(video=0)
                try:
                    packet = next(packet_iter)
                except StopIteration:
                    return
                except OSError as exc:
                    if exc.errno in (None, 4, 11, 35):
                        packet_iter = None
                        self._stop_event.wait(0.001)
                        continue
                    raise
                if packet.size <= 0:
                    continue
                capture_ns = time.monotonic_ns()
                encoded = bytes(packet)
                is_keyframe = bool(getattr(packet, "is_keyframe", False))
                with self._rotation_condition:
                    if self._recording:
                        item = (packet, encoded, capture_ns, is_keyframe)
                        if self._rotation_requested and is_keyframe:
                            self._rotation_keyframe = item
                            self._rotation_condition.notify_all()
                            while self._rotation_requested and not self._stop_event.is_set():
                                self._rotation_condition.wait(timeout=0.2)
                        else:
                            try:
                                self._packet_queue.put_nowait(item)
                            except queue.Full:
                                self._capture_error = "OVISION writer queue overflow"
                                self._emit_health(HealthEvent(
                                    stream_id=self.id,
                                    kind=HealthEventKind.ERROR,
                                    at_ns=capture_ns,
                                    detail=self._capture_error,
                                ))
                if not self._recording:
                    if not self._ready_event.is_set():
                        try:
                            metadata = parse_ovision_h264_metadata(encoded)
                            if not metadata.accel or not metadata.gyro:
                                raise OvisionMetadataError(
                                    "live frame has no accelerometer/gyroscope samples"
                                )
                            self._capture_error = None
                            self._ready_event.set()
                        except OvisionMetadataError as exc:
                            self._capture_error = (
                                f"invalid OVISION live metadata: {exc}"
                            )
                    self._queue_preview(encoded, is_keyframe)
        except Exception as exc:  # noqa: BLE001
            self._capture_error = f"{type(exc).__name__}: {exc}"
            self._emit_health(HealthEvent(
                stream_id=self.id,
                kind=HealthEventKind.ERROR,
                at_ns=time.monotonic_ns(),
                detail=f"OVISION capture loop ended: {self._capture_error}",
            ))

    def _writer_loop(self) -> None:
        while not self._stop_event.is_set() or not self._packet_queue.empty():
            try:
                packet, encoded, capture_ns, is_keyframe = self._packet_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                metadata = parse_ovision_h264_metadata(encoded)
                with self._sink_lock:
                    if self._writer is not None and self._sinks is not None:
                        self._record_packet(packet, metadata, capture_ns, is_keyframe)
            except OvisionMetadataError as exc:
                self._capture_error = f"invalid OVISION frame metadata: {exc}"
                self._emit_health(HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.ERROR,
                    at_ns=capture_ns,
                    detail=self._capture_error,
                ))
            except Exception as exc:  # noqa: BLE001
                self._capture_error = f"OVISION writer failed: {type(exc).__name__}: {exc}"
                self._emit_health(HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.ERROR,
                    at_ns=capture_ns,
                    detail=self._capture_error,
                ))
            finally:
                self._packet_queue.task_done()

    def _record_packet(
        self,
        packet: Any,
        meta: OvisionFrameMetadata,
        capture_ns: int,
        is_keyframe: bool,
    ) -> None:
        assert self._writer is not None and self._sinks is not None
        # MP4 cannot begin on a predictive frame. Keep every artifact aligned
        # by opening the recording window only at the first complete IDR.
        if not self._started_on_keyframe:
            if not is_keyframe:
                return
            self._started_on_keyframe = True
        frame_number = self._frame_count
        device_ns = meta.left_exposure_start_pts_us * 1000
        self._writer.write_packet(packet, capture_ns)
        self._observe_first_frame(capture_ns, device_ns)
        if self._first_at is None:
            self._first_at = capture_ns
        self._last_at = capture_ns
        if self._prev_capture_ns is not None:
            self._intervals_ns.append(capture_ns - self._prev_capture_ns)
        self._prev_capture_ns = capture_ns
        self._frame_count += 1
        self._sinks.frame_count += 1
        self._sinks.frame_meta.write(json.dumps({
            "frame_number": frame_number,
            "capture_ns": capture_ns,
            "clock_source": "device_monotonic",
            "device_timestamp_ns": device_ns,
            "left_exposure_start_ns": meta.left_exposure_start_pts_us * 1000,
            "right_exposure_start_ns": meta.right_exposure_start_pts_us * 1000,
            "stereo_skew_us": meta.stereo_exposure_start_skew_us,
            "left_start_line_rx_ns": meta.left_start_line_rx_pts_us * 1000,
            "right_start_line_rx_ns": meta.right_start_line_rx_pts_us * 1000,
            "left_exposure_time_us": meta.left_exposure_time_us,
            "right_exposure_time_us": meta.right_exposure_time_us,
            "left_gpio_trigger_index": meta.left_gpio_trigger_index,
            "right_gpio_trigger_index": meta.right_gpio_trigger_index,
            "user_data_seq": meta.user_data_seq,
            "frame_meta_generation": meta.frame_meta_generation,
        }, separators=(",", ":")) + "\n")
        self._write_imu(meta, capture_ns)
        self._write_mag(meta, capture_ns)
        self._emit_sample(SampleEvent(
            stream_id=self.id,
            frame_number=frame_number,
            capture_ns=capture_ns,
            uncertainty_ns=500_000,
            device_ns=device_ns,
        ))

    @staticmethod
    def _host_time_for_device(sample_us: int, frame_us: int, capture_ns: int) -> int:
        return capture_ns + (sample_us - frame_us) * 1000

    def _write_imu(self, meta: OvisionFrameMetadata, capture_ns: int) -> None:
        assert self._sinks is not None
        gyro = {sample.device_timestamp_us: sample for sample in meta.gyro}
        accel = {sample.device_timestamp_us: sample for sample in meta.accel}
        if gyro.keys() != accel.keys():
            raise OvisionMetadataError("gyro/accelerometer timestamp sets differ")
        for timestamp_us in sorted(gyro):
            g = gyro[timestamp_us]
            a = accel[timestamp_us]
            gx, gy, gz = g.gyro_rad_s()
            ax, ay, az = a.accel_m_s2()
            sample_capture_ns = self._host_time_for_device(
                timestamp_us, meta.left_exposure_start_pts_us, capture_ns
            )
            common = {
                "frame_number": self._sinks.imu_count,
                "capture_ns": sample_capture_ns,
                "clock_source": "device_monotonic",
                "uncertainty_ns": 500_000,
                "device_timestamp_ns": timestamp_us * 1000,
            }
            self._sinks.imu.write(json.dumps({
                **common,
                "accel_unit": "m_s2",
                "accel_kind": "raw_specific_force_includes_gravity",
                "gyro_unit": "rad_s",
                "channels": {
                    "gyro_x": gx, "gyro_y": gy, "gyro_z": gz,
                    "accel_x": ax, "accel_y": ay, "accel_z": az,
                    "gyro_temperature_raw": g.temperature_raw,
                    "accel_temperature_raw": a.temperature_raw,
                },
            }, separators=(",", ":")) + "\n")
            # Standard raw VIO sidecars.  Acceleration follows the existing
            # OG-Skill raw-accelerometer contract (g); gyro is rad/s.  Keeping
            # these separate prevents OVISION specific force from ever being
            # mistaken for CoreMotion gravity-free DeviceMotion acceleration.
            self._sinks.accel.write(json.dumps({
                **common,
                "frame_number": self._sinks.accel_count,
                "unit": "g",
                "kind": "raw_specific_force_includes_gravity",
                "channels": {
                    "accel_x": ax / 9.80665,
                    "accel_y": ay / 9.80665,
                    "accel_z": az / 9.80665,
                    "temperature_raw": a.temperature_raw,
                },
            }, separators=(",", ":")) + "\n")
            self._sinks.gyro.write(json.dumps({
                **common,
                "frame_number": self._sinks.gyro_count,
                "unit": "rad_s",
                "channels": {
                    "gyro_x": gx, "gyro_y": gy, "gyro_z": gz,
                    "temperature_raw": g.temperature_raw,
                },
            }, separators=(",", ":")) + "\n")
            self._sinks.imu_count += 1
            self._sinks.accel_count += 1
            self._sinks.gyro_count += 1

    def _write_mag(self, meta: OvisionFrameMetadata, capture_ns: int) -> None:
        assert self._sinks is not None
        for sample in meta.mag:
            tx, ty, tz = sample.tesla_20_bit()
            self._sinks.mag.write(json.dumps({
                "frame_number": self._sinks.mag_count,
                "capture_ns": self._host_time_for_device(
                    sample.device_timestamp_us, meta.left_exposure_start_pts_us, capture_ns
                ),
                "clock_source": "device_monotonic",
                "uncertainty_ns": 500_000,
                "device_timestamp_ns": sample.device_timestamp_us * 1000,
                "channels": {
                    "mag_x_raw": sample.xyz_raw[0],
                    "mag_y_raw": sample.xyz_raw[1],
                    "mag_z_raw": sample.xyz_raw[2],
                    "mag_x_tesla_20bit_assumption": tx,
                    "mag_y_tesla_20bit_assumption": ty,
                    "mag_z_tesla_20bit_assumption": tz,
                    "tout_raw": sample.tout_raw,
                    "temperature_milli_c": sample.temperature_milli_c,
                },
            }, separators=(",", ":")) + "\n")
            self._sinks.mag_count += 1

    def _queue_preview(self, encoded: bytes, is_keyframe: bool) -> None:
        now = time.monotonic()
        if not is_keyframe or now - self._last_preview_at < self._preview_interval_s:
            return
        self._last_preview_at = now
        # Single-slot handoff: capture never waits for a multi-megapixel decode.
        self._preview_packet = encoded
        self._preview_wake.set()

    def _preview_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._preview_wake.wait(timeout=0.5):
                continue
            self._preview_wake.clear()
            encoded, self._preview_packet = self._preview_packet, None
            if encoded is None:
                continue
            try:
                decoder = av.CodecContext.create("h264", "r")
                frames = decoder.decode(av.Packet(encoded))
                if frames:
                    packed = frames[-1].to_ndarray(format="bgr24")
                    with self._frame_lock:
                        self._latest_frame = packed[:, : self._width // 2]
            except Exception:  # noqa: BLE001
                logger.debug("OVISION preview decode failed", exc_info=True)

    def _artifacts(self, sinks: _SidecarSinks) -> tuple[OvisionArtifact, ...]:
        return (
            OvisionArtifact(f"{self.id}.stereo", "sensor", sinks.directory / f"{self.id}.stereo.jsonl", sinks.frame_count),
            OvisionArtifact(f"{self.id}.imu", "sensor", sinks.directory / f"{self.id}.imu.jsonl", sinks.imu_count),
            OvisionArtifact(f"{self.id}.accel", "sensor", sinks.directory / f"{self.id}.accel.jsonl", sinks.accel_count),
            OvisionArtifact(f"{self.id}.gyro", "sensor", sinks.directory / f"{self.id}.gyro.jsonl", sinks.gyro_count),
            OvisionArtifact(f"{self.id}.mag", "sensor", sinks.directory / f"{self.id}.mag.jsonl", sinks.mag_count),
        )

    def stop_recording(self) -> FinalizationReport:
        with self._recording_lock:
            self._recording = False
        # The producer can no longer enqueue after the lock handoff above.
        # Drain every already-dequeued frame before closing any sink.
        self._packet_queue.join()
        with self._sink_lock:
            writer, sinks = self._writer, self._sinks
            self._writer = None
            self._sinks = None
        if writer is not None:
            writer.close()
        if sinks is not None:
            sinks.close()
            self._recorded_artifacts = self._artifacts(sinks)
        jitter_p95, jitter_p99 = compute_jitter_percentiles(self._intervals_ns)
        status = "completed" if self._frame_count and not self._capture_error else "failed"
        return FinalizationReport(
            stream_id=self.id,
            status=status,
            frame_count=self._frame_count,
            file_path=self._file_path if self._frame_count else None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=self._capture_error,
            jitter_p95_ns=jitter_p95,
            jitter_p99_ns=jitter_p99,
            recording_anchor=self._recording_anchor(),
        )

    def prepare_segment_rotation(self, next_output_dir: Path) -> None:
        next_output_dir.mkdir(parents=True, exist_ok=True)
        self._write_calibration(next_output_dir)
        path = next_output_dir / f"{self.id}.mp4"
        writer = PassthroughWriter.open(path, template_stream=self._input.streams.video[0])
        sinks = _SidecarSinks.open(next_output_dir, self.id)
        self._prepared = (path, writer, sinks)

    def abort_segment_rotation(self) -> None:
        prepared, self._prepared = self._prepared, None
        if prepared is not None:
            _, writer, sinks = prepared
            writer.close()
            sinks.close()

    def commit_segment_rotation(
        self,
        boundary_monotonic_ns: int,
        swap_persistence: Any = None,
        next_session_clock: SessionClock | None = None,
    ) -> FinalizationReport:
        if self._prepared is None:
            raise RuntimeError("OVISION segment rotation was not prepared")
        # Cut on the next IDR: all earlier packets drain to the old MP4, the IDR
        # itself becomes frame zero in the new MP4. This avoids both packet loss
        # and undecodable segment starts.
        with self._rotation_condition:
            self._rotation_requested = True
            self._rotation_keyframe = None
            deadline = time.monotonic() + 3.0
            while self._rotation_keyframe is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._rotation_requested = False
                    self._rotation_condition.notify_all()
                    raise TimeoutError("OVISION did not emit an IDR within 3 seconds")
                self._rotation_condition.wait(timeout=remaining)
            self._packet_queue.join()
            with self._sink_lock:
                old = (
                    self._writer, self._sinks, self._file_path, self._frame_count,
                    self._first_at, self._last_at, self._intervals_ns, self._recording_anchor(),
                )
                self._file_path, self._writer, self._sinks = self._prepared
                self._output_dir = self._file_path.parent
                self._prepared = None
                self._frame_count = 0
                self._first_at = None
                self._last_at = None
                self._prev_capture_ns = None
                self._intervals_ns = []
                self._started_on_keyframe = False
                if swap_persistence is not None:
                    swap_persistence()
                if next_session_clock is not None:
                    self._begin_recording_window(next_session_clock)
            self._packet_queue.put_nowait(self._rotation_keyframe)
            self._rotation_keyframe = None
            self._rotation_requested = False
            self._rotation_condition.notify_all()
        writer, sinks, path, count, first, last, intervals, anchor = old
        if writer is not None:
            writer.close()
        if sinks is not None:
            sinks.close()
            self._recorded_artifacts = self._artifacts(sinks)
        p95, p99 = compute_jitter_percentiles(intervals)
        return FinalizationReport(
            stream_id=self.id,
            status="completed" if count else "failed",
            frame_count=count,
            file_path=path if count else None,
            first_sample_at_ns=first,
            last_sample_at_ns=last,
            health_events=list(self._collected_health),
            error=None if count else "No OVISION frames arrived during segment",
            jitter_p95_ns=p95,
            jitter_p99_ns=p99,
            recording_anchor=anchor,
        )

    def recorded_artifacts(self) -> tuple[OvisionArtifact, ...]:
        return self._recorded_artifacts

    def disconnect(self) -> None:
        self._stop_event.set()
        self._ready_event.clear()
        with self._rotation_condition:
            self._rotation_requested = False
            self._rotation_condition.notify_all()
        self._preview_wake.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=3)
            self._writer_thread = None
        if self._preview_thread is not None:
            self._preview_thread.join(timeout=3)
            self._preview_thread = None
        if self._input is not None:
            self._input.close()
            self._input = None

    def start(self, session_clock: SessionClock) -> None:
        self.connect()
        self.start_recording(session_clock)

    def stop(self) -> FinalizationReport:
        report = self.stop_recording()
        self.disconnect()
        return report
