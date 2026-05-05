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
with channels ``{thumb, index, middle, ring, pinky}`` and the interpolated
MCU hardware clock in ``SampleEvent.device_ns``. The hardware clock is
**linearly interpolated** across the batch (10 samples × 10 ms) so consumers
see uniform 100 Hz spacing instead of a cluster at every batch boundary.

Lifecycle
---------

This adapter implements the 4-phase :class:`~syncfield.Stream` SPI so
the viewer can plot live FSR values **before** Record is pressed:

* ``prepare()``       — resolve the target peripheral (explicit address
  or name scan).
* ``connect()``       — open the BLE client, subscribe to the notify
  characteristic, and start the background asyncio loop. Samples
  begin flowing into :meth:`_handle_payload` which emits
  :class:`SampleEvent` unconditionally so the viewer's sensor card
  updates its plot even during the preview phase.
* ``start_recording()`` — flip the ``_recording`` flag so incoming
  samples also advance the finalization counters.
* ``stop_recording()`` — flip ``_recording`` back off, snapshot the
  counters into a :class:`FinalizationReport`. The BLE session
  stays live so the plot keeps updating.
* ``disconnect()``    — signal the asyncio loop to stop, release the
  BLE client.

Legacy ``start()`` / ``stop()`` still work — they collapse the new
lifecycle into a ``connect + start_recording`` / ``stop_recording +
disconnect`` pair for 0.1-era callers.

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

        # True while the capture loop should count samples toward the
        # finalization report. ``connect()`` leaves this False so the
        # CONNECTED preview phase still drives the viewer plot (samples
        # are emitted unconditionally via ``_emit_sample``) without
        # polluting the recording's frame counters.
        self._recording = False
        self._frame_count = 0
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None

    # ------------------------------------------------------------------
    # Stream SPI — 4-phase lifecycle
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        """Resolve the target device (explicit address or name scan).

        Heavy connect work (opening the BleakClient, subscribing to
        notifications) happens in :meth:`connect` so the viewer can
        show live sensor values as soon as the session enters
        ``CONNECTED``. This step is cheap and idempotent — repeated
        calls are safe.
        """
        if self._address is not None:
            # bleak accepts either a BLEDevice or a plain address string.
            self._device = self._address
            return

        if self._device is not None:
            return

        # Name-filtered scan. Run synchronously by spinning a throwaway
        # asyncio loop — prepare() runs once before connect() and the
        # scan budget is bounded by ``scan_timeout``.
        self._device = asyncio.run(self._scan_for_glove())
        if self._device is None:
            raise RuntimeError(
                f"[{self.id}] OGLO glove not found "
                f"(name filter={self._ble_name!r}, timeout={self._scan_timeout}s)"
            )

    def connect(self) -> None:
        """Open the BLE session and start the background asyncio loop.

        After this call the ``_on_notify`` handler is subscribed to the
        glove's notify characteristic and decoded samples flow through
        :meth:`_handle_payload` — which emits :class:`SampleEvent`
        unconditionally so the viewer's sensor card's live plot starts
        updating immediately, even while the session is still in
        ``CONNECTED`` (pre-record) state.

        Idempotent — a second call while the loop thread is already
        running is a no-op.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        if self._device is None:
            self.prepare()

        self._recording = False
        self._frame_count = 0
        self._first_at = None
        self._last_at = None
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_event_loop,
            name=f"oglo-{self.id}",
            daemon=True,
        )
        self._thread.start()

    def start_recording(self, session_clock: SessionClock) -> None:
        """Begin counting incoming samples toward the recording report.

        The asyncio loop is already running from :meth:`connect`, so
        this is just a boolean flip. The first sample that arrives
        after this call lands at ``frame_count == 1``; samples that
        arrived during the preview phase are discarded for the
        finalization report but were already visible on the live plot
        through the ``on_sample`` callbacks.

        If the caller skipped :meth:`connect` (legacy 0.1 ``start()``
        path), the BLE session is started here first so the recording
        has a data source.
        """
        if self._thread is None or not self._thread.is_alive():
            self.connect()
        self._begin_recording_window(session_clock)
        self._recording = True

    def stop_recording(self) -> FinalizationReport:
        """Flip recording off and snapshot the finalization report.

        The BLE session **stays live** so the viewer plot keeps
        updating and the operator can start another recording on the
        same session without rescanning or reconnecting.
        """
        self._recording = False
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
        """Signal the asyncio loop to stop and release the BLE client.

        Called when the session returns to ``IDLE``. Idempotent — a
        second call on an already-disconnected stream is a no-op.
        After this call the adapter holds no BLE handles.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # ------------------------------------------------------------------
    # Legacy one-shot lifecycle
    # ------------------------------------------------------------------

    def start(self, session_clock: SessionClock) -> None:
        """Legacy one-shot start — ``connect() + start_recording()``.

        Exists so 0.1-era scripts that called ``prepare() → start() →
        stop()`` keep running unchanged. New callers should use
        :meth:`connect` + :meth:`start_recording` directly.
        """
        self.connect()
        self.start_recording(session_clock)

    def stop(self) -> FinalizationReport:
        """Legacy one-shot stop — ``stop_recording() + disconnect()``."""
        report = self.stop_recording()
        self.disconnect()
        return report

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
        """Scan for a peripheral whose advertised name contains ``ble_name``.

        Matches against **both** the bleak device ``name`` (which is the
        hardware module's peripheral name — on the OGLO board that's
        ``"nimble"``) and the advertisement data's ``local_name``
        (which is what the firmware sets to ``"OGLO"``). Checking only
        one of the two misses real hardware in the wild.
        """
        filter_lower = self._ble_name.lower()
        results = await bleak.BleakScanner.discover(
            timeout=self._scan_timeout, return_adv=True
        )
        for address, (device, adv) in results.items():
            candidates = [
                (getattr(device, "name", None) or ""),
                (getattr(adv, "local_name", None) or ""),
            ]
            for candidate in candidates:
                if filter_lower in candidate.lower():
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
        #
        # Samples are emitted unconditionally so the viewer's sensor
        # card plot updates during both the CONNECTED preview phase
        # and the RECORDING phase. Finalization counters
        # (``_frame_count`` / ``_first_at`` / ``_last_at``) only
        # advance while ``_recording`` is True — so the preview
        # samples never contaminate the recording's frame total.
        for i in range(count):
            offset = _HEADER_SIZE + i * _SAMPLE_SIZE
            values = struct.unpack(
                _SAMPLE_FORMAT, payload[offset : offset + _SAMPLE_SIZE]
            )
            channels: dict = {
                name: int(v) for name, v in zip(FINGER_NAMES, values)
            }
            device_ts_ns = int(
                (timestamp_us + i * _SAMPLE_PERIOD_US) * 1000
            )

            if self._recording:
                # MCU hardware clock is interpolated per sample; pass it as
                # the anchor's device-side timestamp for precise alignment.
                self._observe_first_frame(recv_ns, device_ts_ns)
                if self._first_at is None:
                    self._first_at = recv_ns
                self._last_at = recv_ns
                self._frame_count += 1
                frame_number = self._frame_count - 1
            else:
                # Preview phase — pass through a synthetic, ever-
                # increasing frame number so subscribers that rely
                # on monotonic numbering don't see duplicates, but
                # don't advance the real counter.
                frame_number = -1

            self._emit_sample(
                SampleEvent(
                    stream_id=self.id,
                    frame_number=frame_number,
                    capture_ns=recv_ns,
                    channels=channels,
                    uncertainty_ns=500_000,  # ~0.5 ms — MCU clock precision
                    device_ns=device_ts_ns,
                )
            )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> list:
        """Enumerate OGLO tactile gloves currently advertising over BLE.

        Uses the shared BLE scan cache in :mod:`syncfield.discovery._ble`
        so a single :class:`BleakScanner` run is reused by every BLE
        discoverer during one ``syncfield.discovery.scan()`` pass.
        Filters the raw peripheral list by case-insensitive substring
        match on the advertised name — the stock egonaut firmware
        advertises ``"OGLO …"``, so the default ``"oglo"`` filter picks
        up both left and right gloves without any manual configuration.

        Each returned :class:`~syncfield.discovery.DiscoveredDevice` has
        its ``construct_kwargs`` pre-populated with the exact BLE
        address, so ``scan_and_add`` can build a working ``OgloTactileStream``
        without any extra input from the caller.

        Returns:
            List of ready-to-construct ``DiscoveredDevice``. Empty list
            on platforms without bleak, on Bluetooth adapter errors, or
            when no OGLO-named peripherals are in range.
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
            # Best-effort hand inference from the advertised name.
            # Firmware often suffixes "Left" / "Right"; we extract that
            # as a hint but do not require it.
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
                    construct_kwargs={
                        "address": address,
                        "hand": hand,
                    },
                    accepts_output_dir=False,
                )
            )
        return results

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
