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
        writer = SensorWriter(f"{self.id}.imu", next_output_dir)
        writer.open()
        self._prepared_imu_writer = writer
        mag_writer = SensorWriter(f"{self.id}.mag", next_output_dir)
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
            writer = SensorWriter(f"{self.id}.imu", self._output_dir)
            writer.open()
            self._imu_writer = writer

    def _open_mag_writer(self) -> None:
        with self._imu_lock:
            writer = SensorWriter(f"{self.id}.mag", self._output_dir)
            writer.open()
            self._mag_writer = writer

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
        while time.monotonic() < deadline:
            ser.write(b"GET CONFIG\n")
            ser.flush()
            attempt_end = min(deadline, time.monotonic() + 1.0)
            while time.monotonic() < attempt_end:
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
        """
        ser = None
        try:
            import serial  # lazy: only the wired path needs pyserial

            ser = serial.Serial(self._serial_port, 115200, timeout=0.1)
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
            return

        buffer = b""
        ready = False
        ready_deadline = time.monotonic() + 3.0
        try:
            while not self._stop_event.is_set():
                try:
                    chunk = ser.read(4096)
                except Exception:
                    logger.exception("[%s] USB read failed", self.id)
                    break
                if not chunk:
                    if not ready and time.monotonic() >= ready_deadline:
                        raise OgloProtocolError("OGLO TAG stream produced no valid packet")
                    continue
                buffer += chunk
                packets, buffer = iter_usb_packets(buffer)
                for packet in packets:
                    if not ready:
                        ready = True
                        self._ready_event.set()
                    self._handle_usb_packet(packet)
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
            try:
                ser.write(STREAM_OFF_COMMAND)
                ser.flush()
                ser.close()
            except Exception:
                pass
            self._serial = None

    def _handle_usb_packet(self, packet: UsbTaggedPacket) -> None:
        """Route one independently timestamped schema-6 TAG packet."""
        recv_ns = time.monotonic_ns()
        self._detect_drop(packet.modality, packet.seq, 1, recv_ns)
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

        self._detect_drop("tactile", packet.seq_base, packet.count, recv_ns)

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

    def _detect_drop(self, modality: str, seq_base: int, count: int, recv_ns: int) -> None:
        """Emit recording-window DROP events from the device sequence counter."""
        with self._recording_lock:
            expected = self._next_expected_seq.get(modality)
            recording = self._recording
            self._next_expected_seq[modality] = seq_base + count
        if recording and expected is not None and seq_base > expected:
            missing = seq_base - expected
            self._emit_health(
                HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.DROP,
                    at_ns=recv_ns,
                    detail=f"dropped ~{missing} {modality} samples (seq gap)",
                    data={"missing": missing, "seq_base": seq_base, "modality": modality},
                )
            )

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
