from __future__ import annotations

import asyncio
import time
from typing import Callable

import pytest

from syncfield.adapters.insta360_go3s.ble import protocol as p
from syncfield.adapters.insta360_go3s.ble.camera import (
    CaptureResult,
    Go3SBLECamera,
)


class FakeBleakClient:
    """Minimal in-memory bleak.BleakClient stand-in.

    Records writes; for any CMD_* request, queues a STATUS_OK response with
    the matching seq via the notify callback.
    """

    def __init__(self, address: str):
        self.address = address
        self.is_connected = False
        self._notify_cb: Callable[[int, bytearray], None] | None = None
        self._write_log: list[bytes] = []

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, char_uuid, callback):
        assert char_uuid == p.NOTIFY_CHAR_UUID
        self._notify_cb = callback
        # Send SYNC immediately so the camera's connect() doesn't time out
        sync_outer_no_crc = bytes([0xFF, 0x06, 0x41]) + b"\x07\x00" + b"\x00" * 9
        sync = sync_outer_no_crc + p.crc16_modbus(sync_outer_no_crc).to_bytes(2, "little")
        await asyncio.sleep(0)
        callback(0, bytearray(sync))

    async def stop_notify(self, char_uuid):
        self._notify_cb = None

    async def write_gatt_char(self, char_uuid, data, response=True):
        assert char_uuid == p.WRITE_CHAR_UUID
        self._write_log.append(bytes(data))
        parsed = p.parse_request_packet(bytes(data))
        if parsed is None:
            return
        if parsed.cmd in (
            p.CMD_CHECK_AUTH,
            p.CMD_START_CAPTURE,
            p.CMD_STOP_CAPTURE,
            p.CMD_SET_OPTIONS,
        ):
            resp = self._build_ok_response(parsed.seq, with_filename=parsed.cmd == p.CMD_STOP_CAPTURE)
            assert self._notify_cb is not None
            self._notify_cb(0, bytearray(resp))

    @staticmethod
    def _build_ok_response(seq: int, with_filename: bool) -> bytes:
        # Build inner header: status (LE) at inner[7:9], seq at inner[10].
        inner = bytearray(16)
        inner[4] = 0x04  # mode
        inner[7:9] = p.STATUS_OK.to_bytes(2, "little")  # status, NOT cmd_code
        inner[9] = 0x02  # content_type
        inner[10] = seq
        # Optional ASCII video path embedded in payload (recorder scans data for /DCIM/...)
        pb = b""
        if with_filename:
            pb = b"/DCIM/Camera01/VID_FAKE.mp4\x00"
        payload = bytes(inner) + pb
        outer = (
            bytes([0xFF, 0x06, 0x40])
            + len(payload).to_bytes(2, "little")
            + payload
        )
        return outer + p.crc16_modbus(outer).to_bytes(2, "little")


@pytest.fixture
def fake_client(monkeypatch):
    instances: list[FakeBleakClient] = []

    def factory(device_or_address, *args, **kwargs):
        # macOS path passes a BLEDevice object; Linux/Windows historically
        # accept a str. For tests we accept either and extract an address.
        if isinstance(device_or_address, str):
            address = device_or_address
        else:
            address = getattr(device_or_address, "address", str(device_or_address))
        c = FakeBleakClient(address)
        instances.append(c)
        return c

    monkeypatch.setattr(
        "syncfield.adapters.insta360_go3s.ble.camera.BleakClient",
        factory,
    )

    # Stub BleakScanner.find_device_by_address so the test doesn't try to do
    # a real 8-second BLE scan during connect().
    class _FakeDevice:
        def __init__(self, address: str):
            self.address = address
            self.name = "GO 3S FAKE"

        def __repr__(self) -> str:
            return f"_FakeDevice({self.address!r})"

    class _FakeScanner:
        @staticmethod
        async def find_device_by_address(address, timeout=5.0):
            return _FakeDevice(address)

    monkeypatch.setattr(
        "syncfield.adapters.insta360_go3s.ble.camera.BleakScanner",
        _FakeScanner,
    )
    return instances


@pytest.mark.asyncio
async def test_connect_runs_sync_and_auth(fake_client):
    cam = Go3SBLECamera(address="AA:BB:CC:DD:EE:FF")
    await cam.connect(sync_timeout=2.0, auth_timeout=2.0)
    assert fake_client[0].is_connected
    # First write should be the SYNC response (subtype 0x41)
    first = fake_client[0]._write_log[0]
    assert first[2] == 0x41
    # Subsequent writes should include CMD_CHECK_AUTH at least once
    cmd_codes = []
    for w in fake_client[0]._write_log:
        if w[2] == 0x40:
            req = p.parse_request_packet(w)
            if req is not None:
                cmd_codes.append(req.cmd)
    assert p.CMD_CHECK_AUTH in cmd_codes
    await cam.disconnect()


@pytest.mark.asyncio
async def test_start_capture_returns_host_ns(fake_client):
    cam = Go3SBLECamera(address="AA:BB:CC:DD:EE:FF")
    await cam.connect()
    before = time.monotonic_ns()
    ack_ns = await cam.start_capture()
    after = time.monotonic_ns()
    assert before <= ack_ns <= after
    await cam.disconnect()


@pytest.mark.asyncio
async def test_stop_capture_returns_filepath(fake_client):
    cam = Go3SBLECamera(address="AA:BB:CC:DD:EE:FF")
    await cam.connect()
    await cam.start_capture()
    result: CaptureResult = await cam.stop_capture()
    assert result.file_path == "/DCIM/Camera01/VID_FAKE.mp4"
    await cam.disconnect()
