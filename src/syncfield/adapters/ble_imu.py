"""Profile-driven BLE IMU adapter.

One :class:`BLEImuGenericStream` covers most off-the-shelf BLE IMUs
(WitMotion WT-series, Nordic Thingy, generic NUS firmware, …) by
pushing every vendor-specific detail — frame layout, channel scaling,
one-time configuration commands — into the :class:`BLEImuProfile` the
caller passes at construction time. Curated vendor presets live in
:mod:`syncfield.adapters.ble_imu_profiles`; users with unusual hardware
can build their own profile inline.

The adapter implements the 4-phase :class:`~syncfield.Stream` SPI so
the viewer can plot live IMU values **before** the user hits Record.
Legacy ``start()`` / ``stop()`` wrappers preserve the 0.1-era one-shot
flow for older scripts.

Requires the optional ``ble`` extra::

    pip install 'syncfield[ble]'
"""

from __future__ import annotations

import asyncio
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    import bleak  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover — covered via sys.modules patch
    raise ImportError(
        "BLEImuGenericStream requires bleak. "
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


# ============================================================================
# Profile types — public API for describing a BLE IMU's wire protocol
# ============================================================================


@dataclass(frozen=True)
class ChannelSpec:
    """One decoded channel in a BLE IMU frame.

    Each raw numeric produced by the profile's ``struct_format`` is
    transformed into the value emitted on :class:`SampleEvent` as::

        value = raw * scale + offset

    ``unit`` is documentation only — it is not propagated into emitted
    events, but profile authors should fill it so the preset file is
    self-describing.
    """

    name: str
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""


@dataclass(frozen=True)
class ConfigWrite:
    """One GATT write executed during :meth:`BLEImuGenericStream.connect`.

    Sent via ``BleakClient.write_gatt_char(char_uuid, data)``. A brief
    sleep of ``delay_after_s`` follows each write so vendor firmwares
    that demand inter-command spacing (WitMotion's unlock+rate+save
    dance, for example) don't silently drop the next command.
    """

    char_uuid: str
    data: bytes
    delay_after_s: float = 0.1


@dataclass(frozen=True)
class BLEImuProfile:
    """Declarative description of one BLE IMU's data + config protocol.

    Args:
        notify_uuid: GATT characteristic that emits data frames.
        struct_format: ``struct`` format for **one sample's** body.
        channels: One :class:`ChannelSpec` per value produced by one
            ``struct.unpack(struct_format, …)`` call. Order matches
            ``struct.unpack`` order.
        frame_header: Optional magic-byte prefix placed at the start of
            **each sample** — not just the payload. Vendor firmwares
            commonly bundle multiple timestamp-adjacent samples into a
            single BLE notification by concatenating ``(header + body)``
            sub-frames; with this field set, the adapter validates the
            prefix once per sample instead of once per notification.
            Mismatches become WARNING health events.
        config_writes: Writes executed once per :meth:`connect`, *before*
            ``start_notify``, so the stream sees configured data from
            the very first notification.
        samples_per_frame: Number of samples packed into a single BLE
            notification. ``None`` (default) means "auto-derive from
            payload length", which lets the same profile cover a
            sensor whose firmware adapts its bundling factor to the
            configured output rate (e.g. WitMotion WT901BLE emits 1
            sample/notification at 10 Hz but bundles 8 at 200 Hz).
            Pass an explicit int when the vendor guarantees a fixed
            batch size and you want a length mismatch to surface as a
            warning rather than decode silently.
        sample_period_us: Per-sample spacing in microseconds. Drives
            linear timestamp interpolation across bundled samples so
            downstream consumers see uniform spacing instead of a
            cluster at every notification boundary. Set to
            ``1_000_000 // output_rate_hz`` for fixed-rate sensors;
            leave at ``0`` when only one sample arrives per
            notification (all samples share the receive timestamp).
        description: Human-readable one-liner for logs and preset UIs.
    """

    notify_uuid: str
    struct_format: str
    channels: Tuple[ChannelSpec, ...]
    frame_header: bytes = b""
    config_writes: Tuple[ConfigWrite, ...] = ()
    samples_per_frame: Optional[int] = None
    sample_period_us: int = 0
    description: str = ""

    def __post_init__(self) -> None:
        # Cross-check channel count against what the struct format
        # actually produces per sample.
        probe = struct.unpack(
            self.struct_format,
            b"\x00" * struct.calcsize(self.struct_format),
        )
        if len(self.channels) != len(probe):
            raise ValueError(
                f"BLEImuProfile: {len(self.channels)} channels declared but "
                f"struct_format {self.struct_format!r} produces {len(probe)} "
                f"values per sample"
            )
        if self.samples_per_frame is not None:
            if self.samples_per_frame < 1:
                raise ValueError(
                    f"BLEImuProfile: samples_per_frame must be >= 1 "
                    f"(or None for auto), got {self.samples_per_frame}"
                )
            if self.samples_per_frame > 1 and self.sample_period_us <= 0:
                raise ValueError(
                    f"BLEImuProfile: sample_period_us must be > 0 when "
                    f"samples_per_frame > 1 (got {self.sample_period_us})"
                )

    @property
    def sample_stride(self) -> int:
        """Bytes occupied by one sample (``len(frame_header) + body_size``)."""
        return len(self.frame_header) + struct.calcsize(self.struct_format)


# ============================================================================
# Adapter
# ============================================================================


class BLEImuGenericStream(StreamBase):
    """Profile-driven BLE IMU adapter.

    One adapter class handles every BLE IMU whose wire protocol fits a
    :class:`BLEImuProfile`. Construct with a curated preset from
    :mod:`syncfield.adapters.ble_imu_profiles`, or author a profile
    inline for unlisted hardware.

    Args:
        id: Stream identifier.
        profile: Protocol description (frame layout + config writes).
        address: Explicit BLE address (MAC on Linux/Windows, platform
            UUID on macOS). One of ``address`` / ``ble_name`` is
            required.
        ble_name: Advertised-name substring matched during a scan. Used
            when ``address`` isn't known — :meth:`prepare` picks the
            first peripheral whose name contains this substring.
        scan_timeout: Scan window in seconds when ``ble_name`` is used.
    """

    _discovery_kind = "sensor"
    _discovery_adapter_type = "ble_peripheral"

    def __init__(
        self,
        id: str,
        *,
        profile: BLEImuProfile,
        address: Optional[str] = None,
        ble_name: Optional[str] = None,
        scan_timeout: float = 10.0,
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
        if not address and not ble_name:
            raise ValueError(
                f"[{id}] BLEImuGenericStream needs either 'address' or 'ble_name'"
            )

        self._profile = profile
        self._address = address
        self._ble_name = ble_name
        self._scan_timeout = scan_timeout

        # Hot-path cache — avoids re-deriving per notification.
        self._header = profile.frame_header
        self._header_len = len(profile.frame_header)
        self._sample_fmt = profile.struct_format
        self._body_size = struct.calcsize(profile.struct_format)
        self._sample_stride = profile.sample_stride
        self._samples_per_frame = profile.samples_per_frame  # Optional[int]
        self._sample_period_ns = profile.sample_period_us * 1000
        self._channels = profile.channels

        self._client: Any = None
        self._device: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._recording = False
        self._frame_count = 0
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None

    # ------------------------------------------------------------------
    # 4-phase lifecycle — preferred entry points
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        """Resolve the target peripheral. Idempotent; cheap after the first call.

        The name-filtered scan runs on a throwaway asyncio loop and
        returns a :class:`bleak.BLEDevice`; we extract the ``address``
        string immediately and discard the device object. Holding the
        ``BLEDevice`` across the connect phase would bind us to the
        scanner's loop — when :meth:`connect`'s background thread spins
        up its own loop and hands that stale device to
        :class:`BleakClient`, bleak raises "Future attached to a
        different loop". The raw address string is loop-agnostic and
        :class:`BleakClient` accepts it directly.
        """
        if self._device is not None:
            return
        if self._address is not None:
            self._device = self._address
            return

        scanned = asyncio.run(self._scan_for_device())
        if scanned is None:
            raise RuntimeError(
                f"[{self.id}] BLE peripheral not found "
                f"(name filter={self._ble_name!r}, timeout={self._scan_timeout}s)"
            )
        # Persist only the address — drops loop affinity.
        self._device = getattr(scanned, "address", None) or scanned

    def connect(self) -> None:
        """Open the BLE session, run profile config, subscribe to notifications.

        Samples begin flowing immediately — emitted on ``on_sample`` so
        the viewer plot can preview live values even before recording
        starts. Finalization counters stay frozen until
        :meth:`start_recording` flips them on.
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
            name=f"ble-imu-{self.id}",
            daemon=True,
        )
        self._thread.start()

    def start_recording(self, session_clock: SessionClock) -> None:
        """Begin counting incoming samples toward the finalization report."""
        if self._thread is None or not self._thread.is_alive():
            self.connect()
        self._recording = True

    def stop_recording(self) -> FinalizationReport:
        """Flip recording off, snapshot the report. BLE stays live for re-record."""
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
        )

    def disconnect(self) -> None:
        """Signal the BLE loop to stop and release the client."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # ------------------------------------------------------------------
    # Legacy one-shot lifecycle — kept for 0.1-era callers
    # ------------------------------------------------------------------

    def start(self, session_clock: SessionClock) -> None:
        self.connect()
        self.start_recording(session_clock)

    def stop(self) -> FinalizationReport:
        report = self.stop_recording()
        self.disconnect()
        return report

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
        try:
            self._client = bleak.BleakClient(self._device)
            await self._client.connect()
            await self._apply_config_writes()
            await self._client.start_notify(
                self._profile.notify_uuid, self._on_notify
            )
            while not self._stop_event.is_set():
                await asyncio.sleep(0.05)
            try:
                await self._client.stop_notify(self._profile.notify_uuid)
            except Exception:
                pass
            try:
                await self._client.disconnect()
            except Exception:
                pass
        except Exception as exc:
            self._emit_health(HealthEvent(
                stream_id=self.id,
                kind=HealthEventKind.ERROR,
                at_ns=time.monotonic_ns(),
                detail=str(exc),
            ))

    async def _apply_config_writes(self) -> None:
        """Dispatch each :class:`ConfigWrite` in order, honoring delays."""
        for cw in self._profile.config_writes:
            await self._client.write_gatt_char(cw.char_uuid, cw.data)
            if cw.delay_after_s > 0:
                await asyncio.sleep(cw.delay_after_s)

    async def _scan_for_device(self) -> Any:
        """Name-filtered BLE scan; returns the first matching BLEDevice."""
        name_lower = (self._ble_name or "").lower()
        results = await bleak.BleakScanner.discover(
            timeout=self._scan_timeout, return_adv=True
        )
        for _address, (device, adv) in results.items():
            for candidate in (
                (getattr(device, "name", None) or ""),
                (getattr(adv, "local_name", None) or ""),
            ):
                if name_lower in candidate.lower():
                    return device
        return None

    async def _on_notify(self, characteristic: Any, payload: bytes) -> None:
        self._handle_payload(bytes(payload))

    # ------------------------------------------------------------------
    # Payload decoding — unit-testable, no asyncio/bleak required
    # ------------------------------------------------------------------

    def _handle_payload(self, payload: bytes) -> None:
        """Validate, decode, and emit one BLE notification's worth of samples.

        Accepts notifications carrying any positive integer multiple of
        the sample stride (``len(frame_header) + body_size``). This
        lets a single profile cover sensors whose bundling factor
        varies with the configured output rate — WitMotion WT901BLE,
        for instance, emits 1 sample per notification at 10 Hz but
        packs 8 at 200 Hz. Per-sample timestamps are interpolated
        backward from the receive instant so the *last* sample of the
        batch lands at ``recv_ns`` (the physically correct anchor)
        and earlier samples step backward by ``sample_period_us``.

        A single malformed notification never tears the stream down:
        length or header mismatches become WARNING health events and
        the next notification is decoded normally.
        """
        recv_ns = time.monotonic_ns()
        stride = self._sample_stride

        if len(payload) == 0 or len(payload) % stride != 0:
            self._emit_health(HealthEvent(
                stream_id=self.id,
                kind=HealthEventKind.WARNING,
                at_ns=recv_ns,
                detail=(
                    f"payload length {len(payload)} is not a positive multiple "
                    f"of sample stride {stride}"
                ),
            ))
            return

        n_samples = len(payload) // stride
        if (
            self._samples_per_frame is not None
            and n_samples != self._samples_per_frame
        ):
            self._emit_health(HealthEvent(
                stream_id=self.id,
                kind=HealthEventKind.WARNING,
                at_ns=recv_ns,
                detail=(
                    f"expected {self._samples_per_frame} sample(s) per frame, "
                    f"payload carries {n_samples}"
                ),
            ))
            return

        # First pass — validate every sub-frame's header before emitting
        # anything. A bad header mid-bundle aborts the whole notification
        # so consumers never see a half-decoded batch.
        if self._header:
            for i in range(n_samples):
                sample_start = i * stride
                prefix = payload[sample_start : sample_start + self._header_len]
                if prefix != self._header:
                    self._emit_health(HealthEvent(
                        stream_id=self.id,
                        kind=HealthEventKind.WARNING,
                        at_ns=recv_ns,
                        detail=(
                            f"frame_header mismatch at sample {i}: got "
                            f"{prefix.hex()}, expected {self._header.hex()}"
                        ),
                    ))
                    return

        # Second pass — decode + emit.
        for i in range(n_samples):
            body_start = i * stride + self._header_len
            values = struct.unpack(
                self._sample_fmt, payload[body_start : body_start + self._body_size]
            )
            channels: Dict[str, float] = {
                spec.name: values[j] * spec.scale + spec.offset
                for j, spec in enumerate(self._channels)
            }

            # Anchor the last sample at recv_ns; earlier samples step
            # backward by the configured period. Physically correct:
            # the sensor held earlier samples in its BLE tx buffer
            # before flushing the whole batch to the host.
            sample_ns = recv_ns - (n_samples - 1 - i) * self._sample_period_ns

            if self._recording:
                if self._first_at is None:
                    self._first_at = sample_ns
                self._last_at = sample_ns
                self._frame_count += 1
                frame_number = self._frame_count - 1
            else:
                # Preview phase — emit for the viewer's live plot but
                # don't advance the recording's counters.
                frame_number = -1

            self._emit_sample(SampleEvent(
                stream_id=self.id,
                frame_number=frame_number,
                capture_ns=sample_ns,
                channels=channels,
            ))

    def _dispatch_notification_for_test(self, payload: bytes) -> None:
        """Feed a raw payload through the decode path synchronously (test hook)."""
        self._handle_payload(payload)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> List[Any]:
        """Enumerate generic BLE peripherals as candidate IMUs.

        A generic discoverer cannot know which peripherals are IMUs, so
        each candidate carries a :attr:`warnings` entry explaining that
        the caller must still supply a :class:`BLEImuProfile` before a
        stream can be constructed. ``scan_and_add`` treats non-empty
        warnings as "needs manual attention" and skips them — which is
        what we want: there is no safe generic profile for an arbitrary
        peripheral.

        Peripherals matching a more-specific device-family adapter
        (e.g. OGLO glove, WitMotion WT-series) are filtered out here so
        they appear under one adapter only.
        """
        from syncfield.discovery import DiscoveredDevice
        from syncfield.discovery._ble import scan_peripherals

        peripherals = scan_peripherals(timeout=timeout)

        # Adapters that match specific device families filter themselves
        # in; we exclude their advertised-name prefixes here so a
        # single peripheral shows up under exactly one adapter. Keep
        # substrings narrow — a too-broad token silently hides
        # unrelated peripherals from the picker.
        _EXCLUDE_NAME_SUBSTRINGS = ("oglo", "wt9", "wt8", "hwt9", "hwt8")

        results = []
        for peripheral in peripherals:
            name = (getattr(peripheral, "name", None) or "").strip()
            lowered = name.lower()
            if any(token in lowered for token in _EXCLUDE_NAME_SUBSTRINGS):
                continue

            address = getattr(peripheral, "address", None) or ""
            display_name = name or f"BLE peripheral {address[:8]}"

            results.append(DiscoveredDevice(
                adapter_type="ble_peripheral",
                adapter_cls=cls,
                kind="sensor",
                display_name=display_name,
                description=(
                    f"generic BLE · {address}" if address else "generic BLE"
                ),
                device_id=address or name or display_name,
                construct_kwargs={"address": address},
                accepts_output_dir=False,
                warnings=(
                    "requires a BLEImuProfile for construction — use "
                    "BLEImuGenericStream(profile=…) with a preset from "
                    "syncfield.adapters.ble_imu_profiles or a custom profile",
                ),
            ))
        return results
