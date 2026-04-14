"""Insta360 GO 3S async BLE camera helper.

Public surface:
    CaptureResult   -- dataclass returned by stop_capture()
    Go3SBLECamera   -- connect/start/stop/disconnect lifecycle

Ported from:
    syncfield_recorder/sensors/insta360_ble/camera.py (production-validated)

Key differences from the recorder's GO3SCamera:
    - Address is a str (not a BLEDevice) — the stream layer resolves devices.
    - _send() uses asyncio.Future keyed by seq rather than a shared Event,
      so concurrent (interleaved) commands are safe.
    - No heartbeat loop — the stream layer wraps connect/command/disconnect
      per BLE event, so the connection is short-lived.
    - start_capture() returns ack_host_ns (int, monotonic_ns) instead of
      a BLEResponse, and stop_capture() returns a CaptureResult.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

from bleak import BleakClient, BleakScanner

from syncfield.adapters.insta360_go3s.ble.protocol import (
    CMD_CHECK_AUTH,
    CMD_SET_OPTIONS,
    CMD_START_CAPTURE,
    CMD_STOP_CAPTURE,
    NOTIFY_CHAR_UUID,
    STATUS_OK,
    WRITE_CHAR_UUID,
    build_check_auth_payload,
    build_message_packet,
    build_start_capture_pb,
    build_sync_response,
    build_video_mode_options_pb,
    parse_response,
    parse_response_packet,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Public dataclasses
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class CaptureResult:
    """Result of a stop_capture() call.

    Attributes:
        file_path:    Absolute path to the recorded video file on the camera
                      (e.g. ``/DCIM/Camera01/VID_20240101_120000.mp4``).
        ack_host_ns:  ``time.monotonic_ns()`` captured immediately after the
                      stop-capture ACK was received.
    """

    file_path: str
    ack_host_ns: int


# ──────────────────────────────────────────────────────────────────────────────
# Camera helper
# ──────────────────────────────────────────────────────────────────────────────


class Go3SBLECamera:
    """Controls a single Insta360 GO 3S via BLE.

    Lifecycle::

        cam = Go3SBLECamera("AA:BB:CC:DD:EE:FF")
        await cam.connect()
        ack_ns = await cam.start_capture()
        result = await cam.stop_capture()
        await cam.disconnect()
    """

    def __init__(self, address: str) -> None:
        self._address = address
        self._client: Optional[BleakClient] = None

        # Sequence counter: wraps 1–254 (skip 0 and 255).
        self._seq: int = 1

        # Pending send/receive futures keyed by seq.
        self._pending_acks: Dict[int, asyncio.Future] = {}

        # Set when a SYNC frame is received from the camera.
        self._sync_received_event: asyncio.Event = asyncio.Event()

        # Raw bytes of the last notify frame (kept for video-path scanning).
        self._last_raw: Optional[bytes] = None

        # Flipped by the BleakClient disconnected_callback; lets
        # is_connected report False immediately on async drops.
        self._disconnect_observed: bool = False

        # BLE advertised name, captured during connect(). Used by the stream
        # layer to derive the camera's WiFi AP SSID (pattern: "{ble_name}.OSC").
        self._ble_name: Optional[str] = None

    @property
    def ble_name(self) -> Optional[str]:
        """BLE advertised name captured during connect() (e.g. 'GO 3S 1TEBJJ')."""
        return self._ble_name

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """True when the underlying BleakClient reports a live connection.

        ``_disconnect_observed`` is flipped by the BleakClient disconnect
        callback — if the peripheral dropped the link asynchronously,
        ``self._client.is_connected`` may still momentarily return True, so
        we combine both signals for an accurate view.
        """
        if self._client is None or self._disconnect_observed:
            return False
        return bool(self._client.is_connected)

    def _on_ble_disconnect(self, _client) -> None:
        """BleakClient disconnect callback — marks the link as dead.

        This fires when the camera (or CoreBluetooth) drops the connection
        asynchronously, so that subsequent ``is_connected`` checks correctly
        report the state and the stream layer's ``_ensure_ble_connected``
        can trigger a transparent reconnect.
        """
        logger.warning("[Go3SBLECamera] Peripheral %s disconnected", self._address)
        self._disconnect_observed = True

    # ── public API ────────────────────────────────────────────────────────────

    async def connect(
        self,
        *,
        sync_timeout: float = 2.0,
        auth_timeout: float = 1.0,
        discovery_timeout: float = 8.0,
    ) -> None:
        """Open BLE connection and complete the SYNC + auth handshake.

        Steps:
        1. Resolve the peripheral via BleakScanner.find_device_by_address.
           This is REQUIRED on macOS CoreBluetooth: BleakClient(address_str)
           will time out if CoreBluetooth hasn't seen the peripheral since
           process start. On Linux/Windows the scan is cheap and harmless.
        2. Connect via BleakClient(ble_device).
        3. Subscribe to notifications (this is when the camera sends SYNC).
        4. Wait up to *sync_timeout* for SYNC; if it doesn't arrive, nudge
           the camera with a single ``0x00`` trigger byte.
        5. Send the SYNC response packet (camera expects it).
        6. Send CMD_CHECK_AUTH and wait up to *auth_timeout* for STATUS_OK.
        """
        self._sync_received_event.clear()
        self._pending_acks.clear()
        self._disconnect_observed = False

        # ── Resolve + connect with one retry ────────────────────────────────
        # macOS CoreBluetooth can hold a stale half-connect reference when
        # a prior process crashed mid-handshake. The symptom is that
        # BleakClient.connect() times out even though the peripheral is
        # advertising strongly. Re-doing the scan + fresh BleakClient
        # typically clears the state.
        last_error: Optional[BaseException] = None
        for attempt in (1, 2):
            try:
                device = await BleakScanner.find_device_by_address(
                    self._address, timeout=discovery_timeout
                )
                if device is None:
                    raise RuntimeError(
                        f"Go3S peripheral {self._address!r} not found within "
                        f"{discovery_timeout}s. Verify the camera is powered on, "
                        f"in range, and advertising (check its BLE status light)."
                    )
                logger.debug(
                    "[Go3SBLECamera] Resolved peripheral (attempt %d): %s",
                    attempt,
                    device,
                )
                # Capture BLE name for WiFi SSID derivation upstream.
                name = getattr(device, "name", None)
                if name:
                    self._ble_name = name.strip()

                self._client = BleakClient(
                    device,
                    timeout=15.0,
                    disconnected_callback=self._on_ble_disconnect,
                )
                await self._client.connect()
                logger.debug(
                    "[Go3SBLECamera] Connected to %s on attempt %d",
                    self._address,
                    attempt,
                )
                break
            except Exception as e:
                last_error = e
                logger.warning(
                    "[Go3SBLECamera] Connect attempt %d failed: %s. %s",
                    attempt,
                    e,
                    "Retrying with fresh scan..." if attempt == 1 else "Giving up.",
                )
                # Tear down any half-constructed client so the next scan
                # doesn't race against a zombie.
                if self._client is not None:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                    self._client = None
                if attempt == 2:
                    raise RuntimeError(
                        f"Failed to connect to Go3S {self._address!r} after 2 "
                        f"attempts: {last_error}. If this persists, power-cycle "
                        f"the camera and try again."
                    ) from last_error
                # Small pause before retrying to let CoreBluetooth settle.
                await asyncio.sleep(0.5)

        # Subscribe — FakeBleakClient (and real cameras) emit SYNC here.
        await self._client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)

        # ── Wait for SYNC ──────────────────────────────────────────────────
        try:
            await asyncio.wait_for(
                self._sync_received_event.wait(), timeout=sync_timeout
            )
        except (asyncio.TimeoutError, TimeoutError):
            logger.debug("[Go3SBLECamera] SYNC timeout; sending trigger byte")
            await self._client.write_gatt_char(
                WRITE_CHAR_UUID, bytes([0x00]), response=False
            )
            try:
                await asyncio.wait_for(
                    self._sync_received_event.wait(), timeout=1.0
                )
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning("[Go3SBLECamera] No SYNC received; continuing")

        # ── SYNC response ──────────────────────────────────────────────────
        await self._client.write_gatt_char(
            WRITE_CHAR_UUID, build_sync_response(), response=True
        )

        # ── Auth ───────────────────────────────────────────────────────────
        auth_payload = build_check_auth_payload(self._address)
        try:
            await self._send(CMD_CHECK_AUTH, auth_payload, timeout=auth_timeout)
            logger.debug("[Go3SBLECamera] Auth OK")
        except Exception as exc:
            logger.warning("[Go3SBLECamera] Auth failed: %s", exc)

    async def set_video_mode(self) -> None:
        """Send SET_OPTIONS to enforce video-normal mode (best-effort)."""
        pb = build_video_mode_options_pb()
        await self._send(CMD_SET_OPTIONS, pb, timeout=5.0)
        logger.debug("[Go3SBLECamera] Video mode set")

    async def start_capture(self) -> int:
        """Start recording.

        Returns:
            ack_host_ns: ``time.monotonic_ns()`` captured immediately after
                         the ACK is received (not before the command is sent).
        """
        pb = build_start_capture_pb(mode=1)  # INSCaptureModeNormal
        await self._send(CMD_START_CAPTURE, pb, timeout=5.0)
        ack_host_ns = time.monotonic_ns()
        logger.debug("[Go3SBLECamera] Recording started (ack_ns=%d)", ack_host_ns)
        return ack_host_ns

    async def stop_capture(self) -> CaptureResult:
        """Stop recording.

        Returns:
            CaptureResult with the video file path and ACK timestamp.
        """
        raw = await self._send_raw(CMD_STOP_CAPTURE, b"", timeout=10.0)
        ack_host_ns = time.monotonic_ns()

        # Use the legacy parse_response() which already scans for /DCIM/...
        resp = parse_response(raw) if raw is not None else None
        file_path = (resp.video_path if resp is not None else None) or ""

        logger.debug("[Go3SBLECamera] Recording stopped; file=%s", file_path)
        return CaptureResult(file_path=file_path, ack_host_ns=ack_host_ns)

    async def disconnect(self) -> None:
        """Tear down the BLE connection cleanly."""
        if self._client is not None:
            try:
                await self._client.stop_notify(NOTIFY_CHAR_UUID)
            except Exception:
                pass
            try:
                await self._client.disconnect()
            except Exception:
                pass
        logger.debug("[Go3SBLECamera] Disconnected")

    # ── private helpers ───────────────────────────────────────────────────────

    def _next_seq(self) -> int:
        """Return the next sequence number in [1, 254], wrapping."""
        seq = self._seq
        # Advance: wrap 254 → 1, otherwise increment.
        self._seq = (self._seq % 254) + 1
        return seq

    def _on_notify(self, handle: int, data: bytearray) -> None:
        """BleakClient notification callback — dispatches to pending futures."""
        raw = bytes(data)
        self._last_raw = raw

        if len(raw) < 3 or raw[0] != 0xFF:
            logger.debug("[Go3SBLECamera] Ignoring short/bad notify frame")
            return

        # SYNC frame (subtype 0x41): signal connect() to proceed.
        if raw[2] == 0x41:
            logger.debug("[Go3SBLECamera] SYNC received")
            self._sync_received_event.set()
            return

        # Try structured parse first (gives us seq + status cleanly).
        parsed = parse_response_packet(raw)
        if parsed is not None:
            seq = parsed.seq
            fut = self._pending_acks.get(seq)
            if fut is not None and not fut.done():
                fut.set_result((parsed, raw))
            else:
                logger.debug(
                    "[Go3SBLECamera] Unsolicited or duplicate response seq=%d status=0x%04X",
                    seq,
                    parsed.status,
                )
            return

        # Fallback: legacy parse (handles malformed/short frames).
        legacy = parse_response(raw)
        if legacy is not None and not legacy.is_sync:
            seq = legacy.seq
            fut = self._pending_acks.get(seq)
            if fut is not None and not fut.done():
                fut.set_result((None, raw))

    async def _send(
        self,
        cmd: int,
        payload: bytes,
        timeout: float = 2.0,
    ) -> None:
        """Send a command and wait for a STATUS_OK ACK.

        Raises:
            asyncio.TimeoutError: if no response arrives within *timeout*.
            RuntimeError: if the camera returns a non-OK status code.
        """
        await self._send_raw(cmd, payload, timeout=timeout)

    async def _send_raw(
        self,
        cmd: int,
        payload: bytes,
        timeout: float = 2.0,
    ) -> Optional[bytes]:
        """Send a command, wait for ACK, and return the raw notify bytes.

        Raises:
            asyncio.TimeoutError: if no response arrives within *timeout*.
            RuntimeError: if the camera returns a non-OK status code.
        """
        assert self._client is not None, "Not connected"

        seq = self._next_seq()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_acks[seq] = fut

        pkt = build_message_packet(cmd=cmd, seq=seq, protobuf_payload=payload)
        try:
            await self._client.write_gatt_char(WRITE_CHAR_UUID, pkt, response=True)
            parsed_tuple = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise asyncio.TimeoutError(
                f"No response for cmd=0x{cmd:04X} seq={seq} within {timeout}s"
            ) from exc
        finally:
            self._pending_acks.pop(seq, None)

        parsed, raw = parsed_tuple
        if parsed is not None and parsed.status != STATUS_OK:
            raise RuntimeError(
                f"Camera returned status 0x{parsed.status:04X} for cmd=0x{cmd:04X}"
            )
        return raw
