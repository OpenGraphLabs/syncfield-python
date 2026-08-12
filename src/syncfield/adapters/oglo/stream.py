"""OGLO 0.9.3+ schema-6 USB adapter.

Production capture is wired-only and uses ``STREAM TAG ON``. Tactile, IMU and
magnetometer packets have independent device timestamps and sequence counters;
the primary 80-taxel stream is orchestrator-written while the 500 Hz IMU and
125 Hz magnetometer are persisted as derived substreams.

Design highlights:

The connect handshake stops every legacy stream mode, reads ``GET CONFIG``,
requires firmware >=0.9.3/schema 6 and the expected hand, then waits for a valid
TAG packet before reporting ready. Per-modality sequence gaps are health events.

Requires the optional ``ble`` extra::

    pip install syncfield
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import bleak  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover
    raise ImportError("OgloTactileStream requires the syncfield ble extra") from exc

from syncfield.adapters.oglo.selection import GloveCandidate, select_glove
from syncfield.adapters.oglo.manifest import OgloDeviceManifest, is_supported_firmware
from syncfield.adapters.oglo.packet import OgloProtocolError, parse_v5
from syncfield.adapters.oglo.usb_packet import (
    QUIET_COMMANDS,
    STREAM_OFF_COMMAND,
    STREAM_ON_COMMAND,
    TAG_TYPE_IMU,
    TAG_TYPE_MAG,
    TAG_TYPE_TACTILE,
    UsbTaggedPacket,
    iter_usb_packets,
)
from syncfield.clock import SessionClock
from syncfield.stream import StreamBase
from syncfield.types import (
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    SensorSample,
    StreamCapabilities,
)
from syncfield.writer import SensorWriter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GATT layout (firmware source of truth; base = ASCII "FRS_BLE\0").
# ---------------------------------------------------------------------------
SERVICE_UUID = "4652535f-424c-4500-0000-000000000001"
NOTIFY_CHAR_UUID = "4652535f-424c-4500-0001-000000000001"
CONFIG_CHAR_UUID = "4652535f-424c-4500-0002-000000000001"

#: Per-sample IMU channel order (raw i16 LSB), matching the firmware layout.
IMU_CHANNELS = ("ax", "ay", "az", "gx", "gy", "gz")
MAG_CHANNELS = ("mx", "my", "mz")
# A recording chunk is capped at 30 minutes. Even at the 1 kHz verified IMU
# ceiling it cannot legitimately jump by one million samples between adjacent
# packets. Larger modular deltas mean the byte-stream parser found an embedded
# A5 5A inside a damaged payload, not a real device counter.
_MAX_PLAUSIBLE_SEQUENCE_GAP = 1_000_000

# ESD/EMI can bounce the CDC device off the bus (kernel: "disabled by hub
# (EMI?)", observed on ogpi-005 2026-08-10); the device re-enumerates within
# ~1 s and a stable by-id serial path points at the revived port. The reader
# retries the same path for this window before failing loud — long enough for
# several enumeration cycles plus one USB-reset escalation, short enough that
# a genuinely severed cable still protective-stops the recording quickly.
#
# The reconnect hot path deliberately skips the GET CONFIG round-trip: the
# by-id path embeds the USB serial, so the device answering on that path IS
# this glove, and every avoided handshake second is 250 lost tactile samples.
# Adoption is proven by actual TAG packets instead: a glove whose MCU stayed
# up is still streaming and talks the moment the port opens; a rebooted one
# sits idle until nudged with STREAM TAG ON after a short silence.
_RECONNECT_WINDOW_S = 15.0
_RECONNECT_RETRY_INTERVAL_S = 0.15
# A ready TAG stream delivers 250 Hz tactile plus 500 Hz IMU, so silence this
# long is never normal — it is a dead link that simply has not raised. Nothing
# used to measure it: on ogpi-007 (2026-08-12) a glove went quiet 24 minutes
# into a session, the reader spun on empty reads for another 36 minutes, and
# because the thread stayed alive the host's protective stop never fired. The
# episode kept "recording" one hand. Well above any legitimate gap (rotation
# does not pause the device) and well under the host's own 25 s backstop, so
# the adapter always gets first crack at recovery.
_STREAM_SILENCE_TIMEOUT_S = 5.0
_RECONNECT_STREAM_ON_AFTER_S = 0.4
_RECONNECT_ATTEMPT_DEADLINE_S = 1.6
# An ESP32-S3 CDC stack can wedge outright: still enumerated, port opens,
# firmware mute (three occurrences on ogpi-005, 2026-08-10 — a bus reset
# during active TAG streaming reliably reproduces it). A kernel USBDEVFS_RESET
# forces re-enumeration and revived the glove every time where logical
# replugging did not. Escalate once per outage after this much silence: past
# two adoption attempts (~1.6 s each) and any legitimate enumeration, so a
# healthy blip is never reset, while a wedge self-heals in ~4 s end-to-end.
_RECONNECT_USB_RESET_AFTER_S = 3.0
_USBDEVFS_RESET = (ord("U") << 8) | 20


def _usb_device_reset(serial_port: str, *, stream_id: str = "") -> bool:
    """Best-effort kernel-level reset of the USB device behind *serial_port*.

    Resolves the tty's sysfs device to its bus/device numbers and issues
    USBDEVFS_RESET on the usbfs node. Needs write access to /dev/bus/usb/…
    (the kiosk installer ships a udev rule granting it to ``dialout``).
    Never raises — the reconnect loop just keeps retrying either way.
    """
    try:
        import fcntl

        tty_name = Path(os.path.realpath(serial_port)).name
        interface_dir = Path(os.path.realpath(f"/sys/class/tty/{tty_name}/device"))
        device_dir = interface_dir.parent
        busnum = int((device_dir / "busnum").read_text())
        devnum = int((device_dir / "devnum").read_text())
        node = f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"
        fd = os.open(node, os.O_WRONLY)
        try:
            fcntl.ioctl(fd, _USBDEVFS_RESET, 0)
        finally:
            os.close(fd)
    except Exception as exc:  # noqa: BLE001 - strictly best-effort
        logger.warning(
            "[%s] USB device reset failed for %s: %s", stream_id, serial_port, exc
        )
        return False
    logger.warning("[%s] issued USB device reset for %s", stream_id, serial_port)
    return True

# Keep the USB reader independent from filesystem tail latency. At the default
# rates a 4096-sample queue covers 8.2 s of IMU, 16.4 s of tactile and 32.8 s
# of magnetometer traffic per file. The writer flushes at most every 100 ms,
# reducing the six dual-glove files from ~1750 flushes/s to roughly 60 without
# changing their JSONL schema or ordering.
_OGLO_SENSOR_WRITER_OPTIONS = {
    "queue_capacity": 4096,
    "batch_size": 256,
    "flush_interval_s": 0.1,
}


def _open_usb_cdc(serial_module: Any, port: str, *, timeout: float) -> Any:
    """Open a native ESP32-S3 CDC port without pulsing its reset lines.

    ``pyserial.Serial(port, ...)`` opens immediately with DTR/RTS asserted by
    default.  On the XIAO ESP32-S3 those control-line transitions can reset or
    re-enumerate the native USB device, leaving the just-opened file descriptor
    attached to a stale CDC endpoint.  Configure the lines while the handle is
    still closed, then open it once.  This is also the sequence used by OGLO's
    production firmware tools.
    """
    # ``dtr=False``/``rts=False`` still makes pyserial issue TIOCMBIC during
    # open; ESP32-S3 native CDC returns EPROTO for that ioctl.  Temporarily
    # mark both lines as flow-controlled so pyserial skips both modem-control
    # ioctls, then disable RTS/CTS in termios before the first write.
    ser = serial_module.Serial(
        port=None,
        baudrate=115200,
        timeout=timeout,
        dsrdtr=True,
        rtscts=True,
    )
    ser.dtr = False
    ser.rts = False
    ser.port = port
    ser.open()
    ser.rtscts = False
    return ser


def _manifest_bytes_are_valid_json(raw: bytes) -> bool:
    try:
        json.loads(raw)
        return True
    except (ValueError, UnicodeDecodeError):
        return False


@dataclass(frozen=True)
class OgloSubstream:
    """Descriptor for a derived stream of the OGLO glove (the wrist IMU)."""

    id: str
    kind: str  # "sensor"
    label: str
    expected_hz: float | None = None


@dataclass(frozen=True)
class OgloArtifact:
    """An auxiliary file the adapter wrote besides the primary taxel stream.

    The SDK orchestrator only knows the glove as one stream, so its
    ``manifest.json`` lists just the primary taxels. The desktop backend folds
    these descriptors in after ``stop()`` so the sync pipeline discovers and
    aligns the wrist IMU too.
    """

    stream_id: str
    kind: str  # "sensor"
    path: Path
    frame_count: int


class OgloTactileStream(StreamBase):
    """OGLO tactile glove USB :class:`~syncfield.stream.Stream` adapter.

    Args:
        id: Stream identifier.
        serial_port: CDC-ACM device path. Required.
        hand: Expected ``"left"`` or ``"right"``. The firmware manifest must
            agree or connection fails.
        connect_timeout: Seconds to wait for connect + manifest validation
            before :meth:`connect` gives up.
        output_dir: Directory for the self-written ``{id}.imu.jsonl``. The
            orchestrator rebinds this to the episode dir per recording.
    """

    # Class-level hints for ``syncfield.discovery``.
    _discovery_kind = "sensor"
    _discovery_adapter_type = "oglo_tactile"
    # SessionOrchestrator reads this opt-in hint when it creates the primary
    # tactile writer. Other SyncField sensors retain the synchronous default.
    sensor_writer_options = _OGLO_SENSOR_WRITER_OPTIONS

    def __init__(
        self,
        id: str,
        hand: str = "unknown",
        connect_timeout: float = 10.0,
        output_dir: Path | str | None = None,
        calibration: Optional[dict[str, Any]] = None,
        serial_port: str = "",
    ) -> None:
        super().__init__(
            id=id,
            kind="sensor",
            capabilities=StreamCapabilities(
                provides_audio_track=False,
                # Device provides a per-sample microsecond clock.
                supports_precise_timestamps=True,
                is_removable=True,
                produces_file=False,
            ),
        )
        # Firmware 0.9.3+ production capture is USB CDC only.
        self._serial_port = serial_port.strip() if isinstance(serial_port, str) and serial_port.strip() else None
        if self._serial_port is None:
            raise ValueError(
                f"[{id}] OGLO schema-6 production capture is USB-wired only; serial_port is required"
            )

        self._hand = hand
        self._connect_timeout = connect_timeout
        self._output_dir = Path(output_dir) if output_dir is not None else Path.cwd()
        # Both-hands OGLO extrinsic calibration (``syncfield.oglo_calibration.v1``);
        # this glove writes only its authoritative side on record. See
        # :meth:`_write_calibration_file`.
        self._calibration = calibration

        self._serial: Any = None  # pyserial handle when on the USB transport
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Set once the manifest is read + validated on the reader thread; unblocks
        # connect(). ``_connect_error`` carries any connect/validation failure
        # back to the connect() caller so it can fail loud.
        self._ready_event = threading.Event()
        self._connect_error: Optional[BaseException] = None
        self._manifest: Optional[OgloDeviceManifest] = None
        self._channel_labels: tuple[str, ...] = ()

        # Recording state — primary taxel stream.
        self._recording = False
        self._recording_lock = threading.RLock()
        self._frame_count = 0
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None

        # Derived wrist-IMU substream.
        self._imu_writer: Optional[SensorWriter] = None
        self._mag_writer: Optional[SensorWriter] = None
        self._prepared_imu_writer: Optional[SensorWriter] = None
        self._prepared_mag_writer: Optional[SensorWriter] = None
        self._prepared_output_dir: Optional[Path] = None
        self._imu_lock = threading.Lock()
        self._imu_frame_count = 0
        self._mag_frame_count = 0
        self._imu_path = self._output_dir / f"{id}.imu.jsonl"
        self._mag_path = self._output_dir / f"{id}.mag.jsonl"
        self._recorded_artifacts: tuple[OgloArtifact, ...] = ()
        self._substream_callbacks: list[Callable[[Any], None]] = []
        self._imu_recent_ns: deque[int] = deque(maxlen=120)
        self._mag_recent_ns: deque[int] = deque(maxlen=120)
        self._imu_recent_lock = threading.Lock()

        # Drop detection across packets (seq_base continuity).
        self._next_expected_seq: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Stream SPI — 4-phase lifecycle
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        """The stable serial path is resolved by the host before construction."""

    def connect(self) -> None:
        """Open USB, validate firmware/schema/side, then start TAG capture.

        Blocks until the config manifest has been read and hard-validated
        (``schema_ver == 6``) so a bad-firmware glove fails loud here rather
        than silently producing garbage. Idempotent while the reader lives.
        """
        if self._thread is not None and self._thread.is_alive():
            return

        self._recording = False
        self._frame_count = 0
        self._first_at = None
        self._last_at = None
        self._next_expected_seq = {}
        self._connect_error = None
        self._manifest = None
        self._ready_event.clear()
        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._run_usb_reader, name=f"oglo-usb-{self.id}", daemon=True,
        )
        self._thread.start()

        if not self._ready_event.wait(timeout=self._connect_timeout):
            self._stop_event.set()
            raise RuntimeError(
                f"[{self.id}] OGLO connect timed out after {self._connect_timeout}s "
                "(no config manifest read)"
            )
        if self._connect_error is not None:
            err = self._connect_error
            self.disconnect()
            raise err

    def start_recording(self, session_clock: SessionClock) -> None:
        """Begin counting samples and open the wrist-IMU substream file."""
        if self._thread is None or not self._thread.is_alive():
            self.connect()
        self._rebind_output_paths()
        self._write_calibration_file()
        self._open_imu_writer()
        self._imu_frame_count = 0
        self._open_mag_writer()
        self._mag_frame_count = 0
        # Reset the taxel window state too — these were only reset at
        # connect(), so every consecutive recording in one session (the
        # kiosk's normal duty cycle) reported a CUMULATIVE frame_count
        # (measured_hz 100 -> 200 -> ... over a 6-episode stress run) and
        # emitted SampleEvents whose frame_number continued from the
        # previous episode instead of restarting at 0.
        self._frame_count = 0
        self._first_at = None
        self._last_at = None
        # TAG sequence counters run while the live preview is connected.  Gaps
        # from CDC bring-up or an earlier idle window are not recording loss and
        # must never leak into the next episode's quality report.  Establish a
        # fresh per-modality baseline atomically with the recording transition;
        # the first packet in the window becomes that baseline.
        with self._recording_lock:
            self._next_expected_seq = {}
            self._begin_recording_window(session_clock)
            self._recording = True

    def _write_calibration_file(self) -> None:
        """Write ``{id}.calibration.json`` for this glove's authoritative side.

        Mirrors the OAK composite: the marker + wrist-IMU extrinsics ride into
        the episode next to the tactile/IMU data so SLAM / hand-pose consumers
        get them with zero extra steps. ``self._hand`` is authoritative once the
        manifest is read (post-connect); a wrong pre-connect hint never leaks the
        wrong side. Never raises — calibration must not block a recording.
        """
        from syncfield.oglo_calibration import oglo_side_document

        doc = oglo_side_document(self._calibration, self._hand)
        if doc is None:
            return
        path = self._output_dir / f"{self.id}.calibration.json"
        try:
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning(
                "OGLO %s: failed to write calibration file: %s", self.id, exc
            )

    def stop_recording(self) -> FinalizationReport:
        """Flip recording off, close the IMU file, snapshot the report.

        The USB session stays live so the viewer keeps updating and the
        operator can record again without reopening the port.
        """
        with self._recording_lock:
            self._recording = False
            self._close_imu_writer()
            self._close_mag_writer()
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=self._frame_count,
            file_path=None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=None,
            recording_anchor=self._recording_anchor(),
        )

    def prepare_segment_rotation(self, next_output_dir: Path) -> None:
        next_output_dir.mkdir(parents=True, exist_ok=True)
        writer = self._new_sensor_writer(f"{self.id}.imu", next_output_dir)
        writer.open()
        self._prepared_imu_writer = writer
        mag_writer = self._new_sensor_writer(f"{self.id}.mag", next_output_dir)
        mag_writer.open()
        self._prepared_mag_writer = mag_writer
        self._prepared_output_dir = next_output_dir

        from syncfield.oglo_calibration import oglo_side_document

        doc = oglo_side_document(self._calibration, self._hand)
        if doc is not None:
            (next_output_dir / f"{self.id}.calibration.json").write_text(
                json.dumps(doc, indent=2), encoding="utf-8"
            )

    def abort_segment_rotation(self) -> None:
        writer, self._prepared_imu_writer = self._prepared_imu_writer, None
        mag_writer, self._prepared_mag_writer = self._prepared_mag_writer, None
        self._prepared_output_dir = None
        if writer is not None:
            writer.close()
        if mag_writer is not None:
            mag_writer.close()

    def commit_segment_rotation(
        self,
        boundary_monotonic_ns: int,
        swap_persistence: Any = None,
        next_session_clock: SessionClock | None = None,
    ) -> FinalizationReport:
        if (self._prepared_imu_writer is None or self._prepared_mag_writer is None
                or self._prepared_output_dir is None):
            raise RuntimeError(f"[{self.id}] segment rotation was not prepared")
        with self._recording_lock:
            old_anchor = self._recording_anchor()
            with self._imu_lock:
                old_writer = self._imu_writer
                old_imu_path = self._imu_path
                old_imu_count = self._imu_frame_count
                self._imu_writer = self._prepared_imu_writer
                self._output_dir = self._prepared_output_dir
                self._imu_path = self._output_dir / f"{self.id}.imu.jsonl"
                self._prepared_imu_writer = None
                self._prepared_output_dir = None
                self._imu_frame_count = 0
            with self._imu_lock:
                old_mag_writer = self._mag_writer
                old_mag_path = self._mag_path
                old_mag_count = self._mag_frame_count
                self._mag_writer = self._prepared_mag_writer
                self._mag_path = self._output_dir / f"{self.id}.mag.jsonl"
                self._prepared_mag_writer = None
                self._mag_frame_count = 0
            old_count = self._frame_count
            old_first = self._first_at
            old_last = self._last_at
            self._frame_count = 0
            self._first_at = None
            self._last_at = None
            if swap_persistence is not None:
                swap_persistence()
            if next_session_clock is not None:
                self._begin_recording_window(next_session_clock)
        if old_writer is not None:
            old_writer.close()
        if old_mag_writer is not None:
            old_mag_writer.close()
        artifacts = []
        if old_imu_count > 0:
            artifacts.append(OgloArtifact(f"{self.id}.imu", "sensor", old_imu_path, old_imu_count))
        if old_mag_count > 0:
            artifacts.append(OgloArtifact(f"{self.id}.mag", "sensor", old_mag_path, old_mag_count))
        self._recorded_artifacts = tuple(artifacts)
        return FinalizationReport(
            stream_id=self.id,
            status="completed" if old_count > 0 else "failed",
            frame_count=old_count,
            file_path=None,
            first_sample_at_ns=old_first,
            last_sample_at_ns=old_last,
            health_events=list(self._collected_health),
            error=None if old_count > 0 else "No tactile samples arrived during segment.",
            recording_anchor=old_anchor,
        )

    def disconnect(self) -> None:
        """Stop the TAG reader and release the serial port."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # ------------------------------------------------------------------
    # Legacy one-shot lifecycle
    # ------------------------------------------------------------------

    def start(self, session_clock: SessionClock) -> None:
        """Legacy one-shot start — ``connect() + start_recording()``."""
        self.connect()
        self.start_recording(session_clock)

    def stop(self) -> FinalizationReport:
        """Legacy one-shot stop — ``stop_recording() + disconnect()``."""
        report = self.stop_recording()
        self.disconnect()
        return report

    # ------------------------------------------------------------------
    # Derived-substream surface (wrist IMU) — mirrors the OAK composite
    # ------------------------------------------------------------------

    def substreams(self) -> tuple[OgloSubstream, ...]:
        """Independent inertial modalities carried by schema-6 OGLO."""
        return (
            OgloSubstream(f"{self.id}.imu", "sensor", "Wrist IMU", 500.0),
            OgloSubstream(f"{self.id}.mag", "sensor", "Wrist magnetometer", 125.0),
        )

    def recorded_artifacts(self) -> tuple[OgloArtifact, ...]:
        """Aux files (the wrist IMU) written by the last recording.

        The desktop backend folds these into ``manifest.json`` after ``stop()``
        so the sync pipeline discovers and aligns the wrist IMU, not just the
        primary taxels. Empty until a recording that produced IMU finalizes.
        """
        return self._recorded_artifacts

    def persistence_snapshot(self) -> dict[str, dict[str, int]]:
        """Expose derived-writer queue pressure without touching capture I/O."""

        with self._imu_lock:
            writers = {
                f"{self.id}.imu": self._imu_writer,
                f"{self.id}.mag": self._mag_writer,
            }
        return {
            stream_id: writer.metrics_snapshot()
            for stream_id, writer in writers.items()
            if writer is not None
        }

    def on_substream_sample(self, callback: Callable[[Any], None]) -> None:
        """Register a callback for derived-substream (wrist IMU) samples.

        Deliberately separate from ``on_sample`` so orchestrator sample writers
        never see IMU events — otherwise IMU rows would pollute the primary
        taxel ``{id}.jsonl``.
        """
        self._substream_callbacks.append(callback)

    def substream_capture_hz(self, substream_id: str) -> float:
        """Live capture rate of the wrist-IMU substream."""
        if substream_id == f"{self.id}.imu":
            lock = self._imu_recent_lock
            source = self._imu_recent_ns
        elif substream_id == f"{self.id}.mag":
            lock = self._imu_recent_lock
            source = self._mag_recent_ns
        else:
            return 0.0
        with lock:
            recent = tuple(source)
        if len(recent) < 2:
            return 0.0
        span_s = (recent[-1] - recent[0]) / 1_000_000_000
        return (len(recent) - 1) / span_s if span_s > 0 else 0.0

    def _emit_substream_sample(self, event: Any) -> None:
        for callback in self._substream_callbacks:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - callback isolation
                pass

    # ------------------------------------------------------------------
    # IMU substream file management
    # ------------------------------------------------------------------

    def _rebind_output_paths(self) -> None:
        """Re-derive the IMU file path from the (possibly rotated) output dir.

        The orchestrator rotates ``_output_dir`` for each episode and rebinds
        it via ``_rebind_stream_output_dirs``; calling this at every
        ``start_recording`` makes consecutive recordings idempotently follow
        the current episode dir instead of clobbering the first (mirrors OAK).
        """
        self._imu_path = self._output_dir / f"{self.id}.imu.jsonl"
        self._mag_path = self._output_dir / f"{self.id}.mag.jsonl"

    def _open_imu_writer(self) -> None:
        with self._imu_lock:
            writer = self._new_sensor_writer(f"{self.id}.imu", self._output_dir)
            writer.open()
            self._imu_writer = writer

    def _open_mag_writer(self) -> None:
        with self._imu_lock:
            writer = self._new_sensor_writer(f"{self.id}.mag", self._output_dir)
            writer.open()
            self._mag_writer = writer

    @staticmethod
    def _new_sensor_writer(stream_id: str, output_dir: Path) -> SensorWriter:
        return SensorWriter(stream_id, output_dir, **_OGLO_SENSOR_WRITER_OPTIONS)

    def _close_imu_writer(self) -> None:
        with self._imu_lock:
            writer = self._imu_writer
            count = writer.count if writer is not None else 0
            if writer is not None:
                writer.close()
            self._imu_writer = None
        if count > 0:
            self._recorded_artifacts = (
                OgloArtifact(
                    stream_id=f"{self.id}.imu",
                    kind="sensor",
                    path=self._imu_path,
                    frame_count=count,
                ),
            )
        else:
            self._recorded_artifacts = ()

    def _close_mag_writer(self) -> None:
        with self._imu_lock:
            writer = self._mag_writer
            count = writer.count if writer is not None else 0
            if writer is not None:
                writer.close()
            self._mag_writer = None
        artifacts = list(self._recorded_artifacts)
        if count > 0:
            artifacts.append(
                OgloArtifact(f"{self.id}.mag", "sensor", self._mag_path, count)
            )
        self._recorded_artifacts = tuple(artifacts)

    # ------------------------------------------------------------------
    # Async runtime on the background thread
    # ------------------------------------------------------------------

    def _run_event_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._session())
        finally:
            self._loop.close()

    async def _session(self) -> None:
        """Connect, read+validate the manifest, subscribe, poll stop, cleanup."""
        try:
            self._client = bleak.BleakClient(self._device)
            await self._client.connect()
            raw_manifest = bytes(await self._client.read_gatt_char(CONFIG_CHAR_UUID))
            try:
                manifest = OgloDeviceManifest.from_json(raw_manifest)
            except OgloProtocolError as manifest_exc:
                if _manifest_bytes_are_valid_json(raw_manifest):
                    # The bytes parsed as JSON but failed validation (e.g. an
                    # unsupported schema_ver) — a genuine protocol mismatch, not a
                    # transport artifact. Surface it; do not silently mis-decode.
                    raise
                # The config characteristic can read back TRUNCATED over BLE (an
                # ATT-MTU / firmware buffer limit) even though the glove is a
                # standard schema-5 device — observed on RDR02 REV_D, whose full
                # config is fine over the wired GET CONFIG but is cut ~510 bytes
                # over GATT, yielding invalid JSON. The manifest only supplies the
                # FIXED schema-5 geometry (80 taxels, 5×4×4) plus the side, so fall
                # back to the synthesised default (identical to the wired path)
                # rather than failing the whole connect. The side comes from the
                # hand hint (advertised name / operator selection).
                logger.warning(
                    "[%s] BLE config manifest truncated/unreadable (%s); using schema-5 default",
                    self.id, manifest_exc,
                )
                manifest = self._synthesise_usb_manifest()
            self._apply_manifest(manifest)
        except BaseException as exc:  # noqa: BLE001 - surfaced to connect()
            self._connect_error = exc
            self._ready_event.set()
            self._emit_health(
                HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.ERROR,
                    at_ns=time.monotonic_ns(),
                    detail=f"connect/manifest failed: {exc}",
                )
            )
            return

        # Manifest validated — the stream is ready.
        self._ready_event.set()

        try:
            await self._client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
            while not self._stop_event.is_set():
                await asyncio.sleep(0.05)
            try:
                await self._client.stop_notify(NOTIFY_CHAR_UUID)
            except Exception:
                pass
            try:
                await self._client.disconnect()
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            self._emit_health(
                HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.ERROR,
                    at_ns=time.monotonic_ns(),
                    detail=str(exc),
                )
            )

    async def _on_notify(self, characteristic: Any, payload: bytes) -> None:
        self._handle_payload(bytes(payload))

    async def _scan_for_glove(self) -> Any:
        """Scan, then pick the peripheral that matches ``ble_name`` *and*
        ``hand``.

        This used to return the first peripheral whose name contained
        ``ble_name`` and ignore ``hand`` outright. With both gloves powered on,
        the left and the right stream matched the same device and dict order
        decided which — writing one hand's taxels under the other hand's name.
        Selection now lives in ``selection.select_glove``, which raises rather
        than guess. See that module.
        """
        results = await bleak.BleakScanner.discover(
            timeout=self._scan_timeout, return_adv=True
        )
        candidates = [
            GloveCandidate(
                device,
                [
                    getattr(device, "name", None) or "",
                    getattr(adv, "local_name", None) or "",
                ],
            )
            for _address, (device, adv) in results.items()
        ]
        return select_glove(candidates, ble_name=self._ble_name, hand=self._hand)

    def _apply_manifest(self, manifest: OgloDeviceManifest) -> None:
        """Cache the validated manifest and precompute taxel channel labels.

        The manifest ``side`` is authoritative and overrides the ``hand`` hint.
        """
        self._manifest = manifest
        self._channel_labels = manifest.channel_labels()
        if manifest.side in ("left", "right"):
            self._hand = manifest.side

    # ------------------------------------------------------------------
    # Payload decoding (unit-testable without asyncio / bleak)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # USB CDC transport (wired, preferred)
    # ------------------------------------------------------------------

    def _synthesise_usb_manifest(self) -> OgloDeviceManifest:
        """Schema-6 geometry fallback used only for unit-level construction."""
        side = self._hand if self._hand in ("left", "right") else "unknown"
        return OgloDeviceManifest.from_json(
            json.dumps({"device": "oglo", "side": side, "schema_ver": 6, "rate_hz": 250})
        )

    def _read_usb_manifest(self, ser: Any) -> OgloDeviceManifest:
        ser.write(QUIET_COMMANDS)
        ser.flush()
        time.sleep(0.25)
        ser.reset_input_buffer()
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and not self._stop_event.is_set():
            ser.write(b"GET CONFIG\n")
            ser.flush()
            attempt_end = min(deadline, time.monotonic() + 1.0)
            while time.monotonic() < attempt_end and not self._stop_event.is_set():
                line = ser.readline()
                if not line:
                    continue
                if line.startswith(b"#CONFIG "):
                    manifest = OgloDeviceManifest.from_json(line[len(b"#CONFIG "):].strip())
                    if not is_supported_firmware(manifest.fw_rev):
                        raise OgloProtocolError(
                            "OGLO wired capture requires stable firmware >=0.9.3 "
                            f"with schema 6, got {manifest.fw_rev!r}"
                        )
                    if self._hand in ("left", "right") and manifest.side != self._hand:
                        raise OgloProtocolError(
                            f"OGLO port side={manifest.side!r} does not match requested {self._hand!r}"
                        )
                    return manifest
        raise OgloProtocolError("no #CONFIG response from OGLO USB device")

    def _run_usb_reader(self) -> None:
        """Validate schema-6 config, enable TAG mode, and pump packets.

        Mirrors ``_run_event_loop``'s contract: set ``_ready_event`` once the
        stream is live (or ``_connect_error`` on failure), then loop until
        ``_stop_event``. Each decoded frame goes through ``_handle_usb_frame``,
        which feeds the same ``_emit_sample``/``_handle_imu`` path as BLE.

        A read failure after the stream went ready is treated as a transient
        USB link drop (ESD/EMI bounces the device off the bus for ~1 s) and
        handed to :meth:`_usb_reconnect`; the thread only dies once that
        bounded window is exhausted, so the host's device-liveness view stays
        True across a recovered blip and turns False exactly when recovery is
        no longer possible.
        """
        ser = None
        try:
            import serial  # lazy: only the wired path needs pyserial

            ser = _open_usb_cdc(serial, self._serial_port, timeout=0.1)
            self._serial = ser
            time.sleep(0.2)
            manifest = self._read_usb_manifest(ser)
            self._apply_manifest(manifest)
            ser.reset_input_buffer()
            ser.write(STREAM_ON_COMMAND)
            ser.flush()
        except Exception as exc:  # noqa: BLE001 - report to connect() caller
            self._connect_error = exc
            self._ready_event.set()
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
            self._serial = None
            return

        buffer = b""
        ready = False
        ready_deadline = time.monotonic() + 3.0
        last_packet_at = time.monotonic()

        def recover_or_die(reason: str) -> None:
            """Re-establish the link, or raise so the thread exits.

            Thread death is what the host watches to protective-stop a
            recording, so an unrecoverable link must never leave this loop
            spinning quietly.
            """
            nonlocal ser, buffer, last_packet_at
            reconnected = self._usb_reconnect(reason=reason)
            if reconnected is None:
                raise OgloProtocolError(
                    f"{reason} and reconnect did not succeed "
                    f"within {_RECONNECT_WINDOW_S:.0f}s"
                )
            # Bytes read while proving the stream are real samples — parse
            # them now; waiting for the next chunk would hold them hostage
            # on a quiet link.
            ser, buffer = reconnected
            packets, buffer = iter_usb_packets(buffer)
            for packet in packets:
                self._handle_usb_packet(packet)
            # Adoption already proved a real packet arrived on this handle.
            last_packet_at = time.monotonic()

        try:
            while not self._stop_event.is_set():
                try:
                    chunk = ser.read(4096)
                except Exception:
                    if not ready:
                        raise OgloProtocolError(
                            "OGLO TAG stream stopped before the first packet"
                        )
                    logger.warning(
                        "[%s] USB read failed; attempting reconnect",
                        self.id,
                        exc_info=True,
                    )
                    recover_or_die("USB read failed")
                    continue
                if chunk:
                    buffer += chunk
                    packets, buffer = iter_usb_packets(buffer)
                    handled = 0
                    for packet in packets:
                        if not ready:
                            ready = True
                            self._ready_event.set()
                        self._handle_usb_packet(packet)
                        handled += 1
                    if handled:
                        # Only framed packets count as liveness, and the count
                        # must come from the iteration: `iter_usb_packets`
                        # returns an ITERATOR, and an empty iterator is still
                        # truthy, so `if packets:` refreshed liveness on every
                        # read. A stopped glove keeps emitting `#HB …`
                        # heartbeat text forever, so that made the watchdog
                        # permanently blind (ogpi-005 bench, 2026-08-12).
                        last_packet_at = time.monotonic()
                elif not ready and time.monotonic() >= ready_deadline:
                    raise OgloProtocolError("OGLO TAG stream produced no valid packet")
                silent_s = time.monotonic() - last_packet_at
                if ready and silent_s > _STREAM_SILENCE_TIMEOUT_S:
                    logger.warning(
                        "[%s] no TAG packet for %.1fs; attempting reconnect",
                        self.id,
                        silent_s,
                    )
                    recover_or_die(f"no TAG packet for {silent_s:.1f}s")
            if not ready:
                raise OgloProtocolError("OGLO TAG stream stopped before the first packet")
        except BaseException as exc:  # noqa: BLE001 - surfaced to connect/health
            if not ready:
                self._connect_error = exc
                self._ready_event.set()
            else:
                self._emit_health(HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.ERROR,
                    at_ns=time.monotonic_ns(),
                    detail=f"USB TAG reader failed: {exc}",
                ))
        finally:
            ser = self._serial
            if ser is not None:
                try:
                    ser.write(STREAM_OFF_COMMAND)
                    ser.flush()
                    ser.close()
                except Exception:
                    pass
            self._serial = None

    def _try_adopt_reconnected(self, ser: Any) -> bytes | None:
        """Prove a fresh handle carries a live TAG stream; return its bytes.

        Identity needs no handshake here: the stable by-id path embeds the
        USB serial, so whatever answers on that path is this glove. A glove
        whose MCU stayed up through the link blip is still in TAG mode and
        talks immediately; a rebooted one is idle and gets one STREAM TAG ON
        nudge after a short silence. Returns every byte read (real samples —
        the caller seeds its parse buffer with them), or ``None`` when this
        attempt's deadline passes without a valid packet.
        """
        # Assert TAG mode unconditionally, before reading a single byte. A
        # glove that re-enumerated boots idle and will never speak again
        # unless told to; the old code only nudged when the read buffer was
        # still empty, so one stray byte could leave the device parked while
        # adoption still succeeded on whatever was already in flight. The
        # command is idempotent for a glove that is already streaming.
        try:
            ser.write(STREAM_ON_COMMAND)
            ser.flush()
        except Exception:
            return None

        buffer = b""
        nudged = False
        started = time.monotonic()
        while not self._stop_event.is_set():
            elapsed = time.monotonic() - started
            if elapsed >= _RECONNECT_ATTEMPT_DEADLINE_S:
                return None
            if not nudged and not buffer and elapsed >= _RECONNECT_STREAM_ON_AFTER_S:
                nudged = True
                try:
                    ser.write(STREAM_ON_COMMAND)
                    ser.flush()
                except Exception:
                    return None
            try:
                chunk = ser.read(4096)
            except Exception:
                return None
            if not chunk:
                continue
            buffer += chunk
            packets, _remainder = iter_usb_packets(buffer)
            # `next(..., None)` — NOT `if packets:`. iter_usb_packets returns
            # an ITERATOR, and an empty iterator is truthy, so the old check
            # adopted any handle that produced a single byte. A stopped glove
            # keeps emitting `#HB …` heartbeat text, so reconnect reported
            # success on a link carrying no samples at all: that is how
            # ogpi-007 logged "USB link reconnected after 8443 ms" and then
            # recorded 36 minutes of nothing (2026-08-12).
            if next(packets, None) is not None:
                return buffer
            if len(buffer) > 65_536:
                return None  # a flood that never frames is not a TAG stream
        return None

    def _usb_reconnect(self, reason: str = "USB link lost") -> "tuple[Any, bytes] | None":
        """Reopen the stable serial path after a link drop and resume TAG mode.

        Returns ``(handle, seed_bytes)`` on success, or ``None`` once the
        bounded window is exhausted (or a stop was requested). The sequence
        baseline is reset because a rebooted glove restarts its counters; the
        outage itself is reported explicitly instead, as an estimated DROP.
        A device that is back on the bus but stays mute (wedged ESP32-S3 CDC
        stack) gets one kernel USB reset mid-window — the remedy that revived
        every wedged glove on ogpi-005 where logical replugging did not.
        """
        outage_started = time.monotonic()
        old = self._serial
        self._serial = None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        self._emit_health(HealthEvent(
            stream_id=self.id,
            kind=HealthEventKind.WARNING,
            at_ns=time.monotonic_ns(),
            detail=f"{reason}; reconnecting on {self._serial_port}",
        ))
        import serial as serial_module

        reset_attempted = False
        deadline = outage_started + _RECONNECT_WINDOW_S
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            if (
                not reset_attempted
                and time.monotonic() - outage_started >= _RECONNECT_USB_RESET_AFTER_S
                and os.path.exists(self._serial_port)
            ):
                reset_attempted = True
                _usb_device_reset(self._serial_port, stream_id=self.id)
            ser = None
            try:
                ser = _open_usb_cdc(serial_module, self._serial_port, timeout=0.1)
            except Exception:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
                self._stop_event.wait(_RECONNECT_RETRY_INTERVAL_S)
                continue
            seed = self._try_adopt_reconnected(ser)
            if seed is None:
                try:
                    ser.close()
                except Exception:
                    pass
                continue
            self._serial = ser
            with self._recording_lock:
                # A rebooted device restarts its counters near zero; a stale
                # baseline would make the corrupt-frame guard discard every
                # packet after reconnect, forever.
                self._next_expected_seq = {}
                recording = self._recording
            outage_ms = int((time.monotonic() - outage_started) * 1000)
            logger.warning(
                "[%s] USB link reconnected after %d ms", self.id, outage_ms
            )
            self._emit_health(HealthEvent(
                stream_id=self.id,
                kind=HealthEventKind.WARNING,
                at_ns=time.monotonic_ns(),
                detail=f"USB link reconnected after {outage_ms} ms",
                data={"outage_ms": outage_ms},
            ))
            if recording:
                rate_hz = self._manifest.rate_hz if self._manifest is not None else 250
                estimated = max(0, round(outage_ms / 1000 * rate_hz))
                self._emit_health(HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.DROP,
                    at_ns=time.monotonic_ns(),
                    detail=(
                        f"lost ~{estimated} tactile samples during "
                        f"{outage_ms} ms USB outage (estimated)"
                    ),
                    data={
                        "missing": estimated,
                        "outage_ms": outage_ms,
                        "estimated": True,
                        "modality": "tactile",
                    },
                ))
            return ser, seed
        return None

    def _handle_usb_packet(self, packet: UsbTaggedPacket) -> None:
        """Route one independently timestamped schema-6 TAG packet."""
        recv_ns = time.monotonic_ns()
        if not self._detect_drop(packet.modality, packet.seq, 1, recv_ns):
            return
        if packet.stream_type == TAG_TYPE_IMU:
            self._handle_imu(packet.values, recv_ns, packet.device_ns)
            return
        if packet.stream_type == TAG_TYPE_MAG:
            self._handle_mag(packet.values, recv_ns, packet.device_ns)
            return
        if packet.stream_type != TAG_TYPE_TACTILE:
            return
        labels = self._channel_labels
        device_ns = packet.device_ns
        channels = {labels[i]: int(v) for i, v in enumerate(packet.values)}

        with self._recording_lock:
            if self._recording:
                self._observe_first_frame(recv_ns, device_ns)
                if self._first_at is None:
                    self._first_at = recv_ns
                self._last_at = recv_ns
                self._frame_count += 1
                frame_number = self._frame_count - 1
            else:
                frame_number = -1

            self._emit_sample(
                SampleEvent(
                    stream_id=self.id,
                    frame_number=frame_number,
                    capture_ns=recv_ns,
                    channels=channels,
                    uncertainty_ns=500_000,
                    device_ns=device_ns,
                )
            )

    def _handle_payload(self, payload: bytes) -> None:
        """Decode one packed12 v5 packet into taxel + IMU samples."""
        recv_ns = time.monotonic_ns()
        manifest = self._manifest
        if manifest is None:
            # Notifications should only arrive after connect() validated the
            # manifest; guard defensively so a stray early packet is dropped.
            return

        try:
            packet = parse_v5(payload, values_per_sample=manifest.values_per_sample)
        except OgloProtocolError as exc:
            self._emit_health(
                HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.WARNING,
                    at_ns=recv_ns,
                    detail=f"packet parse failed: {exc}",
                )
            )
            return

        if not self._detect_drop("tactile", packet.seq_base, packet.count, recv_ns):
            return

        labels = self._channel_labels
        for sample in packet.samples:
            device_ns = sample.device_ns
            channels = {labels[i]: int(v) for i, v in enumerate(sample.taxels)}

            with self._recording_lock:
                if self._recording:
                    self._observe_first_frame(recv_ns, device_ns)
                    if self._first_at is None:
                        self._first_at = recv_ns
                    self._last_at = recv_ns
                    self._frame_count += 1
                    frame_number = self._frame_count - 1
                else:
                    frame_number = -1

                self._emit_sample(
                    SampleEvent(
                        stream_id=self.id,
                        frame_number=frame_number,
                        capture_ns=recv_ns,
                        channels=channels,
                        uncertainty_ns=500_000,  # ~0.5 ms device-clock precision
                        device_ns=device_ns,
                    )
                )
                if sample.imu is not None:
                    self._handle_imu(sample.imu, recv_ns, device_ns)

    def _handle_imu(
        self,
        imu: tuple[int, int, int, int, int, int],
        recv_ns: int,
        device_ns: int,
    ) -> None:
        """Fan a wrist-IMU sample out to the live handler + substream file."""
        with self._imu_recent_lock:
            self._imu_recent_ns.append(recv_ns)
        channels = {name: int(v) for name, v in zip(IMU_CHANNELS, imu)}

        with self._recording_lock:
            if self._recording:
                frame_number = self._imu_frame_count
                self._imu_frame_count += 1
                with self._imu_lock:
                    writer = self._imu_writer
                    if writer is not None:
                        writer.write(
                            SensorSample(
                                frame_number=frame_number,
                                capture_ns=recv_ns,
                                channels=channels,
                                uncertainty_ns=500_000,
                                device_timestamp_ns=device_ns,
                            )
                        )
            else:
                frame_number = -1

        self._emit_substream_sample(
            SampleEvent(
                stream_id=f"{self.id}.imu",
                frame_number=frame_number,
                capture_ns=recv_ns,
                channels=channels,
                uncertainty_ns=500_000,
                device_ns=device_ns,
            )
        )

    def _handle_mag(
        self,
        mag: tuple[int, ...],
        recv_ns: int,
        device_ns: int,
    ) -> None:
        """Persist one independently timestamped magnetometer sample."""
        with self._imu_recent_lock:
            self._mag_recent_ns.append(recv_ns)
        channels = {name: int(v) for name, v in zip(MAG_CHANNELS, mag)}
        with self._recording_lock:
            if self._recording:
                frame_number = self._mag_frame_count
                self._mag_frame_count += 1
                with self._imu_lock:
                    writer = self._mag_writer
                    if writer is not None:
                        writer.write(SensorSample(
                            frame_number=frame_number,
                            capture_ns=recv_ns,
                            channels=channels,
                            uncertainty_ns=500_000,
                            device_timestamp_ns=device_ns,
                        ))
            else:
                frame_number = -1
        self._emit_substream_sample(SampleEvent(
            stream_id=f"{self.id}.mag",
            frame_number=frame_number,
            capture_ns=recv_ns,
            channels=channels,
            uncertainty_ns=500_000,
            device_ns=device_ns,
        ))

    def _detect_drop(self, modality: str, seq_base: int, count: int, recv_ns: int) -> bool:
        """Validate sequence continuity and report genuine in-window loss.

        Returns ``False`` for an impossible modular jump so a false header found
        inside a damaged payload cannot enter a tactile/IMU/mag data file.
        """
        with self._recording_lock:
            expected = self._next_expected_seq.get(modality)
            recording = self._recording
            if expected is None:
                self._next_expected_seq[modality] = (seq_base + count) & 0xFFFFFFFF
                return True
            missing = (seq_base - expected) & 0xFFFFFFFF
            corrupt = missing >= _MAX_PLAUSIBLE_SEQUENCE_GAP
            if not corrupt:
                self._next_expected_seq[modality] = (seq_base + count) & 0xFFFFFFFF
        if corrupt:
            if recording:
                self._emit_health(HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.WARNING,
                    at_ns=recv_ns,
                    detail=(
                        f"discarded corrupt {modality} TAG frame "
                        f"(impossible seq {seq_base}, expected {expected})"
                    ),
                    data={
                        "seq_base": seq_base,
                        "expected_seq": expected,
                        "modality": modality,
                    },
                ))
            return False
        if recording and missing:
            self._emit_health(
                HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.DROP,
                    at_ns=recv_ns,
                    detail=f"dropped ~{missing} {modality} samples (seq gap)",
                    data={"missing": missing, "seq_base": seq_base, "modality": modality},
                )
            )
        return True

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> list:
        """Enumerate OGLO gloves currently advertising over BLE.

        Matches the advertised-name substring ``"oglo"`` via the shared BLE
        scan cache. The firmware always advertises an ``OGLO…`` name, so this
        picks up both left and right gloves. The hand is a best-effort hint
        from the name; the manifest ``side`` is authoritative once connected.
        """
        from syncfield.discovery import DiscoveredDevice
        from syncfield.discovery._ble import scan_peripherals

        peripherals = scan_peripherals(timeout=timeout)
        results = []
        for peripheral in peripherals:
            name = (getattr(peripheral, "name", None) or "").strip()
            if "oglo" not in name.lower():
                continue

            address = getattr(peripheral, "address", None) or ""
            lowered = name.lower()
            if "right" in lowered:
                hand = "right"
            elif "left" in lowered:
                hand = "left"
            else:
                hand = "unknown"

            results.append(
                DiscoveredDevice(
                    adapter_type="oglo_tactile",
                    adapter_cls=cls,
                    kind="sensor",
                    display_name=name or "OGLO tactile glove",
                    description=(
                        f"oglo tactile · {hand} · {address[:8]}…"
                        if address
                        else f"oglo tactile · {hand}"
                    ),
                    device_id=address or name,
                    construct_kwargs={"address": address, "hand": hand},
                    accepts_output_dir=False,
                )
            )
        return results

    # ------------------------------------------------------------------
    # Test hooks / properties
    # ------------------------------------------------------------------

    def _dispatch_notification_for_test(self, payload: bytes) -> None:
        """Synchronous entry point for unit tests (no bleak/asyncio required).

        Tests must apply a manifest first (via a real connect or by calling
        :meth:`_apply_manifest`).
        """
        self._handle_payload(payload)

    @property
    def hand(self) -> str:
        """The current hand — manifest ``side`` once connected, else the hint."""
        return self._hand

    @property
    def manifest(self) -> Optional[OgloDeviceManifest]:
        """The validated device manifest, or ``None`` before connect."""
        return self._manifest
