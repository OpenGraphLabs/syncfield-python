"""OgloTactileStream — OGLO tactile glove BLE adapter (firmware v5).

Matches the current OGLO firmware (``FW_REV 0.7.1-cfgfit``, ``schema_ver 5``,
``packed12_v5``), using the ``syncfield-swift`` SDK as the behavioral
reference. The glove streams, over a single notify characteristic, batched
samples of **80 taxels (5×4×4) at 12-bit** plus a **per-sample 6-axis raw
IMU** and a device-clock timestamp. A JSON manifest on the config
characteristic describes the geometry and the authoritative hand.

Design highlights:

* **No command writes.** The device streams at its firmware default the moment
  a central subscribes to the notify CCCD. Command writes were observed to
  destabilize the BLE link, so the stream is consumed as-is and never
  reconfigured at runtime (matches the Swift SDK).
* **Fail-loud on connect.** :meth:`connect` reads the config manifest and
  hard-validates ``schema_ver == 5`` before the stream is considered ready;
  a mismatch raises rather than silently falling back to a legacy parse.
* **Wrist IMU as a derived substream.** The primary stream carries the 80
  taxel channels (``{id}.jsonl`` via the orchestrator's ``SensorWriter``); the
  per-sample IMU is split into a derived ``{id}.imu`` substream the adapter
  self-writes to ``{id}.imu.jsonl``, mirroring the OAK composite so the
  desktop backend folds it into the manifest device-agnostically.

Requires the optional ``ble`` extra::

    pip install 'syncfield[ble]'
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import bleak  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - exercised via sys.modules patch
    raise ImportError(
        "OgloTactileStream requires bleak. "
        "Install with `pip install 'syncfield[ble]'`."
    ) from exc

from syncfield.adapters.oglo.manifest import OgloDeviceManifest
from syncfield.adapters.oglo.packet import OgloProtocolError, parse_v5
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

# ---------------------------------------------------------------------------
# GATT layout (firmware source of truth; base = ASCII "FRS_BLE\0").
# ---------------------------------------------------------------------------
SERVICE_UUID = "4652535f-424c-4500-0000-000000000001"
NOTIFY_CHAR_UUID = "4652535f-424c-4500-0001-000000000001"
CONFIG_CHAR_UUID = "4652535f-424c-4500-0002-000000000001"

#: Per-sample IMU channel order (raw i16 LSB), matching the firmware layout.
IMU_CHANNELS = ("ax", "ay", "az", "gx", "gy", "gz")


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
    """OGLO tactile glove BLE :class:`~syncfield.stream.Stream` adapter.

    Args:
        id: Stream identifier.
        address: Explicit BLE address (or macOS platform UUID). Preferred when
            you already know which glove to connect to. One of ``address`` or
            ``ble_name`` must be supplied.
        ble_name: Advertised-name substring to match during scanning. Default
            ``"oglo"`` (case-insensitive) — the firmware advertises
            ``OGLO`` / ``OGLO LEFT`` / ``OGLO RIGHT``.
        hand: Optional hand hint (``"left"`` | ``"right"`` | ``"unknown"``).
            Only a pre-connect hint — the manifest ``side`` is authoritative
            once connected and overrides it.
        scan_timeout: BLE scan window in seconds when using ``ble_name``.
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
        address: Optional[str] = None,
        ble_name: str = "oglo",
        hand: str = "unknown",
        scan_timeout: float = 10.0,
        connect_timeout: float = 10.0,
        output_dir: Path | str | None = None,
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
        if not address and not ble_name:
            raise ValueError(
                f"[{id}] OgloTactileStream needs either 'address' or 'ble_name'"
            )

        self._address = address
        self._ble_name = ble_name
        self._hand = hand
        self._scan_timeout = scan_timeout
        self._connect_timeout = connect_timeout
        self._output_dir = Path(output_dir) if output_dir is not None else Path.cwd()

        self._client: Any = None
        self._device: Any = None  # BLEDevice from scan, or address string
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Set once the manifest is read + validated on the BLE thread; unblocks
        # connect(). ``_connect_error`` carries any connect/validation failure
        # back to the connect() caller so it can fail loud.
        self._ready_event = threading.Event()
        self._connect_error: Optional[BaseException] = None
        self._manifest: Optional[OgloDeviceManifest] = None
        self._channel_labels: tuple[str, ...] = ()

        # Recording state — primary taxel stream.
        self._recording = False
        self._frame_count = 0
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None

        # Derived wrist-IMU substream.
        self._imu_writer: Optional[SensorWriter] = None
        self._imu_lock = threading.Lock()
        self._imu_frame_count = 0
        self._imu_path = self._output_dir / f"{id}.imu.jsonl"
        self._recorded_artifacts: tuple[OgloArtifact, ...] = ()
        self._substream_callbacks: list[Callable[[Any], None]] = []
        self._imu_recent_ns: deque[int] = deque(maxlen=120)
        self._imu_recent_lock = threading.Lock()

        # Drop detection across packets (seq_base continuity).
        self._next_expected_seq: Optional[int] = None

    # ------------------------------------------------------------------
    # Stream SPI — 4-phase lifecycle
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        """Resolve the target device (explicit address or name scan)."""
        if self._address is not None:
            self._device = self._address
            return
        if self._device is not None:
            return
        self._device = asyncio.run(self._scan_for_glove())
        if self._device is None:
            raise RuntimeError(
                f"[{self.id}] OGLO glove not found "
                f"(name filter={self._ble_name!r}, timeout={self._scan_timeout}s)"
            )

    def connect(self) -> None:
        """Open the BLE session, read + validate the manifest, then subscribe.

        Blocks until the config manifest has been read and hard-validated
        (``schema_ver == 5``) so a bad-firmware glove fails loud here rather
        than silently producing garbage. After this returns the notify
        subscription is active and decoded samples flow through
        :meth:`_handle_payload`. Idempotent while the loop thread is alive.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        if self._device is None:
            self.prepare()

        self._recording = False
        self._frame_count = 0
        self._first_at = None
        self._last_at = None
        self._next_expected_seq = None
        self._connect_error = None
        self._manifest = None
        self._ready_event.clear()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_event_loop,
            name=f"oglo-{self.id}",
            daemon=True,
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
        self._open_imu_writer()
        self._imu_frame_count = 0
        self._begin_recording_window(session_clock)
        self._recording = True

    def stop_recording(self) -> FinalizationReport:
        """Flip recording off, close the IMU file, snapshot the report.

        The BLE session stays live so the viewer plot keeps updating and the
        operator can record again without rescanning.
        """
        self._recording = False
        self._close_imu_writer()
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

    def disconnect(self) -> None:
        """Signal the asyncio loop to stop and release the BLE client."""
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
        """Derived streams carried in the glove's notify packets (wrist IMU)."""
        return (OgloSubstream(f"{self.id}.imu", "sensor", "Wrist IMU"),)

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
        if substream_id != f"{self.id}.imu":
            return 0.0
        with self._imu_recent_lock:
            recent = tuple(self._imu_recent_ns)
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

    def _open_imu_writer(self) -> None:
        with self._imu_lock:
            writer = SensorWriter(f"{self.id}.imu", self._output_dir)
            writer.open()
            self._imu_writer = writer

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
            raw_manifest = await self._client.read_gatt_char(CONFIG_CHAR_UUID)
            self._apply_manifest(OgloDeviceManifest.from_json(bytes(raw_manifest)))
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
        """Scan for a peripheral whose advertised name contains ``ble_name``."""
        filter_lower = self._ble_name.lower()
        results = await bleak.BleakScanner.discover(
            timeout=self._scan_timeout, return_adv=True
        )
        for _address, (device, adv) in results.items():
            candidates = [
                (getattr(device, "name", None) or ""),
                (getattr(adv, "local_name", None) or ""),
            ]
            for candidate in candidates:
                if filter_lower in candidate.lower():
                    return device
        return None

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

        self._detect_drop(packet.seq_base, packet.count, recv_ns)

        labels = self._channel_labels
        for sample in packet.samples:
            device_ns = sample.device_ns
            channels = {labels[i]: int(v) for i, v in enumerate(sample.taxels)}

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

    def _detect_drop(self, seq_base: int, count: int, recv_ns: int) -> None:
        """Emit a DROP health event on a gap in the device sequence counter."""
        expected = self._next_expected_seq
        if expected is not None and seq_base > expected:
            missing = seq_base - expected
            self._emit_health(
                HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.DROP,
                    at_ns=recv_ns,
                    detail=f"dropped ~{missing} samples (seq gap)",
                    data={"missing": missing, "seq_base": seq_base},
                )
            )
        self._next_expected_seq = seq_base + count

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
