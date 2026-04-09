"""BLEImuGenericStream — generic BLE IMU reference adapter using ``bleak``.

Connects to a BLE peripheral by MAC address (or platform-specific UUID on
macOS) and subscribes to a single notify characteristic. Each notification
payload is parsed with a user-provided :mod:`struct` format string and
emitted as a :class:`~syncfield.types.SampleEvent` with per-channel values.

Because ``bleak`` is an asyncio library and the orchestrator API is
synchronous, this adapter spins up an :class:`asyncio.AbstractEventLoop`
on an internal background thread. The main thread and the loop thread
communicate via a :class:`threading.Event` to signal stop.

Requires the optional ``ble`` extra:

    pip install syncfield[ble]
"""

from __future__ import annotations

import asyncio
import struct
import threading
import time
from typing import Any, Optional, Tuple

try:
    import bleak  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - exercised via sys.modules patch
    raise ImportError(
        "BLEImuGenericStream requires bleak. "
        "Install with `pip install syncfield[ble]`."
    ) from exc

from syncfield.clock import SessionClock
from syncfield.stream import StreamBase
from syncfield.types import (
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    StreamCapabilities,
)


class BLEImuGenericStream(StreamBase):
    """Generic BLE IMU adapter.

    Args:
        id: Stream id.
        mac: Peripheral MAC address (or platform-specific UUID on macOS).
        characteristic_uuid: UUID of the notify characteristic.
        frame_format: ``struct`` format for decoding notification payloads.
            Default ``"<fffffff"`` — 7 little-endian floats (accel x/y/z,
            gyro x/y/z, temperature).
        channel_names: Names to use as channel keys in emitted
            :class:`SampleEvent`\\ s. Must match the number of values
            produced by ``frame_format``.
    """

    # Class-level hints for ``syncfield.discovery``. ``ble_peripheral`` is
    # used as the adapter_type (rather than ``ble_imu``) because the
    # generic discoverer returns *any* BLE peripheral — not just IMUs —
    # as a candidate; the user must still supply a characteristic_uuid
    # to turn one into a working stream.
    _discovery_kind = "sensor"
    _discovery_adapter_type = "ble_peripheral"

    DEFAULT_FORMAT = "<fffffff"
    DEFAULT_CHANNELS: Tuple[str, ...] = ("ax", "ay", "az", "gx", "gy", "gz", "temp")

    def __init__(
        self,
        id: str,
        mac: str,
        characteristic_uuid: str,
        frame_format: str = DEFAULT_FORMAT,
        channel_names: Tuple[str, ...] = DEFAULT_CHANNELS,
    ) -> None:
        super().__init__(
            id=id,
            kind="sensor",
            capabilities=StreamCapabilities(
                provides_audio_track=False,
                supports_precise_timestamps=True,
                is_removable=True,
                produces_file=False,
            ),
        )
        self._mac = mac
        self._uuid = characteristic_uuid
        self._format = frame_format
        self._channel_names = channel_names

        # Compute how many values the format produces by unpacking a
        # zero-filled buffer of the correct size.
        produced = struct.unpack(frame_format, b"\x00" * struct.calcsize(frame_format))
        if len(channel_names) != len(produced):
            raise ValueError(
                f"channel_names has {len(channel_names)} entries but format "
                f"{frame_format!r} produces {len(produced)} values"
            )

        self._client: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._frame_count = 0
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None

    # ------------------------------------------------------------------
    # Stream SPI
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        self._client = bleak.BleakClient(self._mac)

    def start(self, session_clock: SessionClock) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_event_loop,
            name=f"ble-{self.id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> FinalizationReport:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=self._frame_count,
            file_path=None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=None,
        )

    # ------------------------------------------------------------------
    # Async runtime on the background thread
    # ------------------------------------------------------------------

    def _run_event_loop(self) -> None:
        """Body of the background thread — owns a private asyncio loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._session())
        finally:
            self._loop.close()

    async def _session(self) -> None:
        """Connect, subscribe, poll the stop flag, then disconnect."""
        try:
            await self._client.connect()
            await self._client.start_notify(self._uuid, self._on_notify)
            while not self._stop_event.is_set():
                await asyncio.sleep(0.05)
            await self._client.stop_notify(self._uuid)
            await self._client.disconnect()
        except Exception as exc:
            self._emit_health(
                HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.ERROR,
                    at_ns=time.monotonic_ns(),
                    detail=str(exc),
                )
            )

    async def _on_notify(self, characteristic: Any, payload: bytes) -> None:
        """Bleak notify handler — forwards to the sync decode path."""
        self._handle_payload(payload)

    # ------------------------------------------------------------------
    # Payload decoding (unit-testable without asyncio)
    # ------------------------------------------------------------------

    def _handle_payload(self, payload: bytes) -> None:
        """Decode a raw BLE payload into a :class:`SampleEvent` and emit.

        Decode failures become WARNING health events rather than raising
        so a single malformed notification cannot tear down the stream.
        """
        capture_ns = time.monotonic_ns()
        try:
            values = struct.unpack(self._format, payload)
        except struct.error as exc:
            self._emit_health(
                HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.WARNING,
                    at_ns=capture_ns,
                    detail=f"payload decode failed: {exc}",
                )
            )
            return

        if self._first_at is None:
            self._first_at = capture_ns
        self._last_at = capture_ns
        self._frame_count += 1

        self._emit_sample(
            SampleEvent(
                stream_id=self.id,
                frame_number=self._frame_count - 1,
                capture_ns=capture_ns,
                channels=dict(zip(self._channel_names, values)),
            )
        )

    def _dispatch_notification_for_test(self, payload: bytes) -> None:
        """Test-only hook: push a payload through the decode path synchronously."""
        self._handle_payload(payload)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> list:
        """Enumerate generic BLE peripherals as candidate IMUs.

        Unlike the other BLE-based adapters (e.g.
        :class:`~syncfield.adapters.OgloTactileStream`), this discoverer
        can't know which of the advertising peripherals are actually
        IMUs. It returns *every* peripheral it sees and marks each one
        with a :attr:`~syncfield.discovery.DiscoveredDevice.warnings`
        entry explaining that the caller still needs to supply a
        ``characteristic_uuid`` (and possibly a custom ``frame_format``)
        before a stream can be constructed.

        ``scan_and_add`` treats devices with non-empty warnings as
        "needs manual attention" and skips them — which is the correct
        behavior here, because there is no generic way to determine the
        notify characteristic of an arbitrary peripheral. Users wiring
        a real BLE IMU should construct the adapter explicitly with the
        UUID they learned from the device datasheet or a BLE explorer.

        Peripherals that match a more-specific adapter (e.g. ``oglo``
        for :class:`OgloTactileStream`) are filtered out here so they
        don't appear twice in the discovery report.
        """
        from syncfield.discovery import DiscoveredDevice
        from syncfield.discovery._ble import scan_peripherals

        peripherals = scan_peripherals(timeout=timeout)

        # Adapters that match specific device families filter themselves
        # in; we exclude those here so a single peripheral shows up under
        # one adapter only. Keep this list short — if you add a new
        # device-family adapter, add its name filter substring here.
        _EXCLUDE_NAME_SUBSTRINGS = ("oglo",)

        results = []
        for peripheral in peripherals:
            name = (getattr(peripheral, "name", None) or "").strip()
            lowered = name.lower()
            if any(token in lowered for token in _EXCLUDE_NAME_SUBSTRINGS):
                continue

            address = getattr(peripheral, "address", None) or ""
            display_name = name or f"BLE peripheral {address[:8]}"

            results.append(
                DiscoveredDevice(
                    adapter_type="ble_peripheral",
                    adapter_cls=cls,
                    kind="sensor",
                    display_name=display_name,
                    description=(
                        f"generic BLE · {address}" if address else "generic BLE"
                    ),
                    device_id=address or name or display_name,
                    construct_kwargs={"mac": address},
                    accepts_output_dir=False,
                    warnings=(
                        "requires characteristic_uuid for construction — "
                        "use BLEImuGenericStream(characteristic_uuid=…) manually",
                    ),
                )
            )
        return results
