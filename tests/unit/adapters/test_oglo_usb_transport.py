"""OgloTactileStream USB CDC transport — driven by a fake serial port.

No real hardware, no pyserial socket: a `_FakeSerial` replays canned bytes and
records what the stream wrote back (the STREAM BIN ON/OFF commands).
"""

from __future__ import annotations

import importlib
import struct
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from syncfield.adapters.oglo.usb_packet import USB_MAGIC, USB_FRAME_LEN
from syncfield.clock import SessionClock
from syncfield.types import SyncPoint

NUM_TAXELS = 80


def make_usb_frame(ts_us: int, taxel0: int = 0, imu_ok: int = 1) -> bytes:
    taxels = [0] * NUM_TAXELS
    taxels[0] = taxel0
    body = bytearray(USB_MAGIC)
    body += struct.pack("<I", ts_us)
    body += struct.pack(f"<{NUM_TAXELS}H", *taxels)
    body += struct.pack("<8h", 5000, -2000, 10, 20, 30, 1, 2, 3)  # roll,pitch,ax..gz
    body += bytes([imu_ok])
    assert len(body) == USB_FRAME_LEN
    return bytes(body)


class _FakeSerial:
    """Minimal pyserial.Serial stand-in. Feeds `frames_bytes` out of read(),
    records writes, and stops after the canned bytes are drained."""

    def __init__(self, port, baud, timeout=0.1):
        self.port = port
        self.writes = []
        self._to_read = b""
        self._lock = threading.Lock()
        self.closed = False

    def feed(self, data: bytes):
        with self._lock:
            self._to_read += data

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        with self._lock:
            if self._to_read:
                chunk = self._to_read[:n]
                self._to_read = self._to_read[n:]
                return chunk
        time.sleep(0.005)
        return b""

    def close(self):
        self.closed = True


@pytest.fixture
def oglo_usb(monkeypatch):
    """Import the stream module with fake bleak + a fake `serial` module."""
    fake_bleak = MagicMock()
    monkeypatch.setitem(sys.modules, "bleak", fake_bleak)

    holder = {}

    def _serial_ctor(port, baud, timeout=0.1):
        s = _FakeSerial(port, baud, timeout)
        holder["serial"] = s
        return s

    fake_serial_mod = SimpleNamespace(Serial=_serial_ctor)
    monkeypatch.setitem(sys.modules, "serial", fake_serial_mod)

    for m in ("syncfield.adapters.oglo", "syncfield.adapters.oglo.stream"):
        sys.modules.pop(m, None)
    module = importlib.import_module("syncfield.adapters.oglo")
    yield module, holder
    for m in ("syncfield.adapters.oglo", "syncfield.adapters.oglo.stream"):
        sys.modules.pop(m, None)


def test_serial_port_selects_usb_transport(oglo_usb):
    module, _ = oglo_usb
    s = module.OgloTactileStream("tactile_left", serial_port="/dev/ttyACM1", hand="left")
    assert s._serial_port == "/dev/ttyACM1"
    # A wired stream needs no address/name.
    assert s._address is None


def test_empty_serial_port_falls_back_to_address_requirement(oglo_usb):
    module, _ = oglo_usb
    # No serial, no address, no ble_name -> the old error.
    with pytest.raises(ValueError, match="serial_port"):
        module.OgloTactileStream("x", serial_port="", address="", ble_name="")


def test_connect_enables_binary_stream_and_reads_frames(oglo_usb):
    module, holder = oglo_usb
    samples = []
    s = module.OgloTactileStream("tactile_right", serial_port="/dev/ttyACM0", hand="right")
    s.on_sample(lambda ev: samples.append(ev))

    s.connect()
    fake = holder["serial"]
    # It must have enabled the binary stream.
    assert any(b"STREAM BIN ON" in w for w in fake.writes)

    # Feed three frames; the reader thread should decode them.
    fake.feed(make_usb_frame(1000) + make_usb_frame(2000) + make_usb_frame(3000))
    deadline = time.monotonic() + 2.0
    while len(samples) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    s.disconnect()

    assert len(samples) >= 3
    # Taxel channels use the same labels as BLE (finger_row_col).
    assert any(k.startswith(("thumb", "index", "middle", "ring", "pinky")) for k in samples[0].channels)
    # STREAM BIN OFF sent on the way out.
    assert any(b"STREAM BIN OFF" in w for w in fake.writes)
    assert fake.closed


def test_wired_recording_writes_taxels_and_wrist_imu(oglo_usb, tmp_path):
    module, holder = oglo_usb
    s = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/ttyACM1", hand="left", output_dir=tmp_path
    )
    s.connect()
    clock = SessionClock(sync_point=SyncPoint.create_now("pi5"), recording_armed_ns=1_000_000)
    s.start_recording(clock)

    holder["serial"].feed(b"".join(make_usb_frame(1000 + i * 10000, taxel0=i) for i in range(5)))
    deadline = time.monotonic() + 2.0
    while s._frame_count < 5 and time.monotonic() < deadline:
        time.sleep(0.02)
    report = s.stop_recording()
    s.disconnect()

    assert s._frame_count >= 5
    # The wrist-IMU substream file was written.
    imu_path = tmp_path / "tactile_left.imu.jsonl"
    assert imu_path.is_file()
    assert imu_path.read_text().strip(), "wrist IMU jsonl is non-empty"
    assert report is not None


def test_bad_serial_port_reports_error_from_connect(oglo_usb, monkeypatch):
    module, _ = oglo_usb

    def _boom(port, baud, timeout=0.1):
        raise OSError(f"could not open {port}")

    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=_boom))
    s = module.OgloTactileStream("tactile_left", serial_port="/dev/ttyBAD", hand="left")
    with pytest.raises(OSError, match="could not open"):
        s.connect()
