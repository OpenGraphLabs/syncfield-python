"""USB link-loss auto-reconnect for the OGLO schema-6 wired adapter.

Field reality (ogpi-005, 2026-08-10): ESD/EMI from a gloved operator drops the
CDC device off the bus for ~1 s and the kernel re-enumerates it immediately.
Before reconnect existed, that one-second blip killed the reader thread and the
episode silently lost every remaining tactile sample.

The reconnect hot path skips the GET CONFIG handshake — identity is pinned by
the stable ``/dev/serial/by-id`` path, which embeds the USB serial — and proves
adoption with actual TAG packets instead, so the data gap stays near the USB
re-enumeration floor. A device that is back but mute (wedged ESP32-S3 CDC
stack) gets one kernel USB reset mid-window.
"""

import importlib
import struct
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from syncfield.adapters.oglo.usb_packet import TAG_MAGIC, TAG_TYPE_TACTILE
from syncfield.clock import SessionClock
from syncfield.types import HealthEventKind, SyncPoint


def tag(kind: int, seq: int, t_us: int) -> bytes:
    payload = struct.pack("<80H", *range(80))
    return TAG_MAGIC + bytes([kind]) + struct.pack("<HII", len(payload), seq, t_us) + payload


class _SerialError(Exception):
    """Stands in for serial.SerialException without importing pyserial."""


class _FakeSerial:
    """Streams queued TAG packets, then optionally dies like a USB unplug.

    ``stream_on_gated=True`` models a glove whose MCU rebooted during the
    outage: it stays silent until the host writes STREAM TAG ON.
    """

    def __init__(self, port, *, stream_on_gated=False):
        self.port, self.writes, self.closed = port, [], False
        self.dtr = True
        self.rts = True
        self.dsrdtr = False
        self.rtscts = False
        self.timeout = 0.1
        self._lock = threading.Lock()
        self._to_read = b""
        self._dead = False
        self._gated = stream_on_gated
        self.fail_open = False

    def open(self):
        if self.fail_open:
            raise _SerialError(f"could not open port {self.port}")

    def feed(self, data):
        with self._lock:
            self._to_read += data

    def kill(self):
        """Subsequent read() raises, like the kernel tearing down the tty."""
        with self._lock:
            self._dead = True

    def _stream_enabled(self):
        return any(b"STREAM TAG ON" in w for w in self.writes)

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        with self._lock:
            if self._dead:
                raise _SerialError(
                    "device reports readiness to read but returned no data"
                )
            if self._to_read and (not self._gated or self._stream_enabled()):
                out, self._to_read = self._to_read[:n], self._to_read[n:]
                return out
        time.sleep(0.002)
        return b""

    def readline(self):
        import json

        cfg = {
            "device": "oglo", "schema_ver": 6, "side": "left",
            "serial": "OGLO-TEST-L", "fw_rev": "0.9.3", "rate_hz": 250,
        }
        return b"#CONFIG " + json.dumps(cfg).encode() + b"\n"

    def close(self):
        self.closed = True


@pytest.fixture
def oglo_usb(monkeypatch):
    """Import the adapter against a serial module whose Serial() pulls the next
    fake from a queue — one per (re)connect attempt."""
    monkeypatch.setitem(sys.modules, "bleak", MagicMock())
    holder = {"queue": [], "created": []}

    def ctor(port=None, baudrate=115200, timeout=0.1, dsrdtr=False, rtscts=False):
        if holder["queue"]:
            fake = holder["queue"].pop(0)
        else:
            fake = _FakeSerial(port)
        holder["created"].append(fake)
        return fake

    monkeypatch.setitem(
        sys.modules, "serial", SimpleNamespace(Serial=ctor, SerialException=_SerialError)
    )
    for name in ("syncfield.adapters.oglo", "syncfield.adapters.oglo.stream"):
        sys.modules.pop(name, None)
    module = importlib.import_module("syncfield.adapters.oglo")
    yield module, holder
    for name in ("syncfield.adapters.oglo", "syncfield.adapters.oglo.stream"):
        sys.modules.pop(name, None)


def _clock():
    return SessionClock(sync_point=SyncPoint.create_now("pi5"), recording_armed_ns=1_000_000)


def _wait(predicate, timeout_s=4.0):
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate(), "condition not met in time"


def _connected_recording_stream(module, holder, tmp_path, first):
    first.feed(tag(TAG_TYPE_TACTILE, 0, 1_000))
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/serial/by-id/oglo-left", hand="left",
        output_dir=tmp_path,
    )
    health = []
    stream.on_health(health.append)
    stream.connect()
    stream.start_recording(_clock())
    first.feed(tag(TAG_TYPE_TACTILE, 1, 2_000))
    _wait(lambda: stream._frame_count >= 1)
    return stream, health


def test_still_streaming_glove_is_adopted_without_any_handshake(oglo_usb, tmp_path):
    """MCU survived the blip: TAG frames flow the moment the port reopens.
    No GET CONFIG round-trip may appear on the new handle — every avoided
    handshake second is 250 lost tactile samples."""
    module, holder = oglo_usb
    first, second = _FakeSerial("p"), _FakeSerial("p")
    second.feed(tag(TAG_TYPE_TACTILE, 500, 60_000))
    holder["queue"] = [first, second]

    stream, health = _connected_recording_stream(module, holder, tmp_path, first)
    first.kill()
    _wait(lambda: stream._frame_count >= 2)

    assert stream._thread.is_alive(), "reader must survive a link blip"
    assert not any(b"GET CONFIG" in w for w in second.writes), (
        "reconnect hot path must not spend time on a config handshake"
    )
    kinds = [h.kind for h in health]
    assert HealthEventKind.WARNING in kinds
    assert any("reconnect" in h.detail.lower() for h in health)

    report = stream.stop_recording()
    stream.disconnect()
    assert report.frame_count == 2


def test_rebooted_glove_gets_a_stream_on_nudge(oglo_usb, tmp_path):
    """MCU rebooted during the outage: silent until STREAM TAG ON."""
    module, holder = oglo_usb
    first = _FakeSerial("p")
    second = _FakeSerial("p", stream_on_gated=True)
    second.feed(tag(TAG_TYPE_TACTILE, 3, 9_000))
    holder["queue"] = [first, second]

    stream, _health = _connected_recording_stream(module, holder, tmp_path, first)
    first.kill()
    _wait(lambda: stream._frame_count >= 2)

    assert any(b"STREAM TAG ON" in w for w in second.writes)
    stream.stop_recording()
    stream.disconnect()


def test_reconnect_reports_outage_as_estimated_drop(oglo_usb, tmp_path):
    module, holder = oglo_usb
    first, second = _FakeSerial("p"), _FakeSerial("p")
    second.feed(tag(TAG_TYPE_TACTILE, 7, 9_000))
    holder["queue"] = [first, second]

    stream, health = _connected_recording_stream(module, holder, tmp_path, first)
    first.kill()
    _wait(lambda: stream._frame_count >= 2)
    stream.stop_recording()
    stream.disconnect()

    drops = [h for h in health if h.kind is HealthEventKind.DROP]
    assert len(drops) == 1
    assert drops[0].data.get("estimated") is True
    assert drops[0].data.get("outage_ms", -1) >= 0


def test_reconnect_resets_seq_baseline_so_reboot_does_not_poison_stream(oglo_usb, tmp_path):
    """A rebooted glove restarts seq near 0. Without a baseline reset the
    corrupt-frame guard would discard every packet after reconnect, forever."""
    module, holder = oglo_usb
    first, second = _FakeSerial("p"), _FakeSerial("p")
    second.feed(tag(TAG_TYPE_TACTILE, 3, 9_000))
    holder["queue"] = [first, second]

    first.feed(tag(TAG_TYPE_TACTILE, 0x7FFF0000, 500))
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/serial/by-id/oglo-left", hand="left",
        output_dir=tmp_path,
    )
    stream.connect()
    stream.start_recording(_clock())
    first.feed(tag(TAG_TYPE_TACTILE, 0x7FFF0001, 1_000))
    _wait(lambda: stream._frame_count >= 1)
    first.kill()
    _wait(lambda: stream._frame_count >= 2)
    stream.stop_recording()
    stream.disconnect()
    assert stream._frame_count == 2


def test_reconnect_gives_up_after_window_and_thread_exits(oglo_usb, tmp_path, monkeypatch):
    module, holder = oglo_usb
    monkeypatch.setattr(module.stream, "_RECONNECT_WINDOW_S", 0.4)
    monkeypatch.setattr(module.stream, "_RECONNECT_RETRY_INTERVAL_S", 0.05)

    first = _FakeSerial("p")
    first.feed(tag(TAG_TYPE_TACTILE, 0, 1_000))

    def dead_ctor(port=None, baudrate=115200, timeout=0.1, dsrdtr=False, rtscts=False):
        fake = _FakeSerial(port)
        fake.fail_open = True
        return fake

    holder["queue"] = [first]
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/serial/by-id/oglo-left", hand="left",
        output_dir=tmp_path,
    )
    health = []
    stream.on_health(health.append)
    stream.connect()
    # After the first connect, every later Serial() fails to open (link gone).
    sys.modules["serial"].Serial = dead_ctor

    first.kill()
    _wait(lambda: not stream._thread.is_alive())

    errors = [h for h in health if h.kind is HealthEventKind.ERROR]
    assert errors, "an exhausted reconnect window must fail loud"
    assert any("reconnect" in e.detail.lower() for e in errors)
    stream.disconnect()


def test_present_but_mute_device_escalates_to_one_usb_reset(oglo_usb, tmp_path, monkeypatch):
    """The wedge case: enumerated, port opens, firmware mute. Three real
    occurrences on ogpi-005 (2026-08-10); a kernel USBDEVFS_RESET revived the
    glove every time where logical replugging did not — so the reconnect loop
    fires exactly one reset per outage once the device is present but silent."""
    module, holder = oglo_usb
    monkeypatch.setattr(module.stream, "_RECONNECT_WINDOW_S", 1.2)
    monkeypatch.setattr(module.stream, "_RECONNECT_RETRY_INTERVAL_S", 0.05)
    monkeypatch.setattr(module.stream, "_RECONNECT_ATTEMPT_DEADLINE_S", 0.15)
    monkeypatch.setattr(module.stream, "_RECONNECT_STREAM_ON_AFTER_S", 0.05)
    monkeypatch.setattr(module.stream, "_RECONNECT_USB_RESET_AFTER_S", 0.3)
    monkeypatch.setattr(module.stream.os.path, "exists", lambda _p: True)
    resets = []
    monkeypatch.setattr(
        module.stream, "_usb_device_reset",
        lambda port, stream_id="": resets.append(port) or True,
    )

    first = _FakeSerial("p")
    first.feed(tag(TAG_TYPE_TACTILE, 0, 1_000))
    holder["queue"] = [first]  # every later open yields a fresh, mute fake

    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/serial/by-id/oglo-left", hand="left",
        output_dir=tmp_path,
    )
    stream.connect()
    first.kill()
    _wait(lambda: not stream._thread.is_alive())

    assert resets == ["/dev/serial/by-id/oglo-left"], (
        "exactly one USB reset per outage"
    )
    stream.disconnect()
