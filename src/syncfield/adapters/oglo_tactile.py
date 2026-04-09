"""OgloTactileStream — OGLO tactile glove BLE reference adapter.

Ports the BLE protocol from the egonaut iOS app
(``egonaut/mobile/ios/EgonautMobile/Tactile/TactileGloveManager.swift``)
into a :class:`~syncfield.stream.Stream` SDK adapter. The glove exposes a
single notify characteristic that streams batched 5-finger FSR samples
with a hardware-clock timestamp.

Protocol summary (ported from ``TactileConstants.swift``):

- **Service UUID**      : ``4652535f-424c-4500-0000-000000000001``
- **Notify characteristic** : ``4652535f-424c-4500-0001-000000000001``
- **Config characteristic** : ``4652535f-424c-4500-0002-000000000001``
  (optional — returns a JSON manifest with side + per-channel locations)
- **Packet layout** (little-endian):

  =======  ======  =====================================================
  Offset   Type    Meaning
  =======  ======  =====================================================
  [0:2]    u16     count — samples in this batch (typically 10)
  [2:6]    u32     timestamp_us — MCU hardware clock at start of batch
  [6:...]  5×u16   per-sample: thumb, index, middle, ring, pinky (each)
  =======  ======  =====================================================

- **Sample rate**: 100 Hz effective (≈10 notifications/second × 10 samples)
- **Scan filter**: advertised name substring ``"oglo"`` (case-insensitive)

Each decoded sample is emitted as one :class:`~syncfield.types.SampleEvent`
with channels ``{thumb, index, middle, ring, pinky, device_timestamp_ns}``.
The MCU hardware clock is **linearly interpolated** across the batch
(10 samples × 10 ms) so consumers see uniform 100 Hz spacing instead of a
cluster at every batch boundary — critical for downstream jitter analysis.

Requires the optional ``ble`` extra::

    pip install 'syncfield[ble]'
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
        "OgloTactileStream requires bleak. "
        "Install with `pip install 'syncfield[ble]'`."
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


# ---------------------------------------------------------------------------
# Protocol constants (copied from egonaut's TactileConstants.swift)
# ---------------------------------------------------------------------------

SERVICE_UUID = "4652535f-424c-4500-0000-000000000001"
NOTIFY_CHAR_UUID = "4652535f-424c-4500-0001-000000000001"
CONFIG_CHAR_UUID = "4652535f-424c-4500-0002-000000000001"

#: Canonical per-finger channel order, matching the iOS app's emitted
#: preview. Left and right gloves use the same order; orientation is a
#: property of the ``hand`` kwarg the caller supplies.
FINGER_NAMES: Tuple[str, ...] = ("thumb", "index", "middle", "ring", "pinky")

_HEADER_FORMAT = "<HI"                              # count (u16), ts_us (u32)
_HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)      # 6 bytes
_SAMPLE_FORMAT = "<5H"                              # 5 × u16 FSR readings
_SAMPLE_SIZE = struct.calcsize(_SAMPLE_FORMAT)      # 10 bytes
_SAMPLE_PERIOD_US = 10_000                          # 100 Hz → 10 ms per sample


class OgloTactileStream(StreamBase):
    """OGLO tactile glove BLE :class:`~syncfield.stream.Stream` adapter.

    Maintains a persistent BLE connection for the duration of a recording
    session and emits one :class:`SampleEvent` per decoded sample —
    which, at 100 Hz and 10 samples per batch, means ~10 notifications
    fan out into ~100 ``SampleEvent``\\ s per second.

    Args:
        id: Stream identifier.
        address: Explicit BLE address (or macOS platform UUID). Preferred
            when you already know which glove to connect to. One of
            ``address`` or ``ble_name`` must be supplied.
        ble_name: Advertised-name substring to match during scanning.
            Default ``"oglo"`` (case-insensitive), which works for any
            glove running the stock egonaut firmware.
        hand: Optional hand label — ``"left"`` | ``"right"`` | ``"unknown"``.
            Stored verbatim in the :class:`FinalizationReport` extras but
            does not affect protocol parsing (canonical thumb/index/... order
            is used regardless).
        scan_timeout: BLE scan timeout in seconds when using ``ble_name``.
            Default ``10.0``.
    """

    def __init__(
        self,
        id: str,
        address: Optional[str] = None,
        ble_name: str = "oglo",
        hand: str = "unknown",
        scan_timeout: float = 10.0,
    ) -> None:
        super().__init__(
            id=id,
            kind="sensor",
            capabilities=StreamCapabilities(
                provides_audio_track=False,
                # The MCU provides a hardware microsecond clock that we
                # interpolate per-sample, so timestamps are genuinely precise.
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

        self._client: Any = None
        self._device: Any = None  # BLEDevice from scan, or address string
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
        """Resolve the target device (explicit address or name scan)."""
        if self._address is not None:
            # bleak accepts either a BLEDevice or a plain address string.
            self._device = self._address
            return

        # Name-filtered scan. Run synchronously by spinning a throwaway
        # asyncio loop — prepare() is called once, before the capture
        # loop starts, so it's OK to block here briefly.
        self._device = asyncio.run(self._scan_for_glove())
        if self._device is None:
            raise RuntimeError(
                f"[{self.id}] OGLO glove not found "
                f"(name filter={self._ble_name!r}, timeout={self._scan_timeout}s)"
            )

    def start(self, session_clock: SessionClock) -> None:
        """Kick off the background asyncio loop that drives the BLE client."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_event_loop,
            name=f"oglo-{self.id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> FinalizationReport:
        """Signal the loop to exit and collect the finalization report."""
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
        """Connect, subscribe, poll the stop flag, then disconnect cleanly."""
        try:
            self._client = bleak.BleakClient(self._device)
            await self._client.connect()
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
        self._handle_payload(bytes(payload))

    async def _scan_for_glove(self) -> Any:
        """Scan for a peripheral whose advertised name contains ``ble_name``."""
        filter_lower = self._ble_name.lower()
        devices = await bleak.BleakScanner.discover(timeout=self._scan_timeout)
        for device in devices:
            name = (getattr(device, "name", None) or "").lower()
            if filter_lower in name:
                return device
        return None

    # ------------------------------------------------------------------
    # Payload decoding (unit-testable without asyncio / bleak)
    # ------------------------------------------------------------------

    def _handle_payload(self, payload: bytes) -> None:
        """Decode one batched packet into N per-sample ``SampleEvent``\\ s.

        Short or truncated packets become ``WARNING`` health events rather
        than raising, so a single malformed notification cannot tear down
        the stream.
        """
        recv_ns = time.monotonic_ns()
        if len(payload) < _HEADER_SIZE:
            self._emit_health(
                HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.WARNING,
                    at_ns=recv_ns,
                    detail=f"short packet: {len(payload)} bytes",
                )
            )
            return

        count, timestamp_us = struct.unpack(_HEADER_FORMAT, payload[:_HEADER_SIZE])
        body_size = count * _SAMPLE_SIZE
        if len(payload) < _HEADER_SIZE + body_size:
            self._emit_health(
                HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.WARNING,
                    at_ns=recv_ns,
                    detail=(
                        f"truncated: header count={count}, "
                        f"got {len(payload)} bytes, "
                        f"expected ≥ {_HEADER_SIZE + body_size}"
                    ),
                )
            )
            return

        # Emit one SampleEvent per per-finger sample in the batch at the
        # full 100 Hz rate. The MCU hardware clock is linearly interpolated
        # across the batch so downstream consumers see uniform 10 ms
        # spacing instead of a cluster at every batch boundary.
        for i in range(count):
            offset = _HEADER_SIZE + i * _SAMPLE_SIZE
            values = struct.unpack(
                _SAMPLE_FORMAT, payload[offset : offset + _SAMPLE_SIZE]
            )
            channels: dict = {
                name: int(v) for name, v in zip(FINGER_NAMES, values)
            }
            channels["device_timestamp_ns"] = int(
                (timestamp_us + i * _SAMPLE_PERIOD_US) * 1000
            )

            if self._first_at is None:
                self._first_at = recv_ns
            self._last_at = recv_ns
            self._frame_count += 1

            self._emit_sample(
                SampleEvent(
                    stream_id=self.id,
                    frame_number=self._frame_count - 1,
                    capture_ns=recv_ns,
                    channels=channels,
                    uncertainty_ns=500_000,  # ~0.5 ms — MCU clock precision
                )
            )

    # ------------------------------------------------------------------
    # Test hooks
    # ------------------------------------------------------------------

    def _dispatch_notification_for_test(self, payload: bytes) -> None:
        """Synchronous entry point used by unit tests (no bleak required)."""
        self._handle_payload(payload)

    @property
    def hand(self) -> str:
        """The hand label supplied at construction time."""
        return self._hand
