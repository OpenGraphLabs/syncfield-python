"""USB link-loss auto-reconnect for the OGLO schema-6 wired adapter.

Field reality (ogpi-005, 2026-08-10): ESD/EMI from a gloved operator drops the
CDC device off the bus for ~1 s and the kernel re-enumerates it immediately.
Before reconnect existed, that one-second blip killed the reader thread and the
episode silently lost every remaining tactile sample. These tests drive the
reader with fake serials that die mid-stream and assert the stream resumes on
the same (stable by-id) path, records the outage as health events, and still
fails loud when the link never returns.
"""

import importlib
import json
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
    """Streams queued TAG packets, then optionally dies like a USB unplug."""

    def __init__(self, port):
        self.port, self.writes, self.closed = port, [], False
        self.dtr = True
        self.rts = True
        self.dsrdtr = False
        self.rtscts = False
        self.timeout = 0.1
        self._lock = threading.Lock()
        self._to_read = b""
        self._dead = False
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

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def readline(self):
        cfg = {
            "device": "oglo", "schema_ver": 6, "side": "left",
            "serial": "OGLO-TEST-L", "fw_rev": "0.9.3", "rate_hz": 250,
        }
        return b"#CONFIG " + json.dumps(cfg).encode() + b"\n"

    def read(self, n):
        with self._lock:
            if self._dead:
                raise _SerialError(
                    "device reports readiness to read but returned no data"
                )
            if self._to_read:
                out, self._to_read = self._to_read[:n], self._to_read[n:]
                return out
        time.sleep(0.002)
        return b""

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


def _wait(predicate, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate(), "condition not met in time"


def test_reader_reconnects_after_link_loss_and_stream_resumes(oglo_usb, tmp_path):
    module, holder = oglo_usb
    first, second = _FakeSerial("p"), _FakeSerial("p")
    first.feed(tag(TAG_TYPE_TACTILE, 0, 1_000))
    holder["queue"] = [first, second]

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

    first.kill()  # EMI blip: tty torn down mid-recording
    second.feed(tag(TAG_TYPE_TACTILE, 500, 60_000))
    _wait(lambda: stream._frame_count >= 2, timeout_s=5.0)

    assert stream._thread.is_alive(), "reader must survive a link blip"
    assert any(b"STREAM TAG ON" in w for w in second.writes), (
        "reconnect must re-enable TAG mode on the new handle"
    )
    kinds = [h.kind for h in health]
    assert HealthEventKind.WARNING in kinds, "link loss must be recorded"
    assert any("reconnect" in h.detail.lower() for h in health)

    report = stream.stop_recording()
    stream.disconnect()
    assert report.frame_count == 2


def test_reconnect_reports_outage_as_estimated_drop(oglo_usb, tmp_path):
    module, holder = oglo_usb
    first, second = _FakeSerial("p"), _FakeSerial("p")
    holder["queue"] = [first, second]

    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/serial/by-id/oglo-left", hand="left",
        output_dir=tmp_path,
    )
    health = []
    stream.on_health(health.append)
    first.feed(tag(TAG_TYPE_TACTILE, 0, 1_000))
    stream.connect()
    stream.start_recording(_clock())
    first.feed(tag(TAG_TYPE_TACTILE, 1, 2_000))
    _wait(lambda: stream._frame_count >= 1)

    first.kill()
    second.feed(tag(TAG_TYPE_TACTILE, 7, 9_000))
    _wait(lambda: stream._frame_count >= 2, timeout_s=5.0)
    stream.stop_recording()
    stream.disconnect()

    drops = [h for h in health if h.kind is HealthEventKind.DROP]
    assert len(drops) == 1
    assert drops[0].data.get("estimated") is True
    assert drops[0].data.get("outage_ms", -1) >= 0


def test_reconnect_resets_seq_baseline_so_reboot_does_not_poison_stream(oglo_usb, tmp_path):
    """A glove that rebooted restarts seq near 0. Without a baseline reset the
    corrupt-frame guard would discard every packet after reconnect, forever."""
    module, holder = oglo_usb
    first, second = _FakeSerial("p"), _FakeSerial("p")
    holder["queue"] = [first, second]

    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/serial/by-id/oglo-left", hand="left",
        output_dir=tmp_path,
    )
    first.feed(tag(TAG_TYPE_TACTILE, 0x7FFF0000, 500))
    stream.connect()
    stream.start_recording(_clock())
    # High seq before the outage; near-zero after (device rebooted).
    first.feed(tag(TAG_TYPE_TACTILE, 0x7FFF0001, 1_000))
    _wait(lambda: stream._frame_count >= 1)
    first.kill()
    second.feed(tag(TAG_TYPE_TACTILE, 3, 2_000))
    _wait(lambda: stream._frame_count >= 2, timeout_s=5.0)
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
    _wait(lambda: not stream._thread.is_alive(), timeout_s=5.0)

    errors = [h for h in health if h.kind is HealthEventKind.ERROR]
    assert errors, "an exhausted reconnect window must fail loud"
    assert any("reconnect" in e.detail.lower() for e in errors)
    stream.disconnect()


def test_reconnect_refuses_a_different_glove_on_the_same_path(oglo_usb, tmp_path, monkeypatch):
    """by-id paths make this near-impossible, but a wrong glove must never be
    silently adopted mid-episode — that would interleave two devices' data."""
    module, holder = oglo_usb
    monkeypatch.setattr(module.stream, "_RECONNECT_WINDOW_S", 0.6)
    monkeypatch.setattr(module.stream, "_RECONNECT_RETRY_INTERVAL_S", 0.05)

    first = _FakeSerial("p")
    holder["queue"] = [first]

    def wrong_ctor(port=None, baudrate=115200, timeout=0.1, dsrdtr=False, rtscts=False):
        fake = _FakeSerial(port)
        cfg = {
            "device": "oglo", "schema_ver": 6, "side": "left",
            "serial": "OGLO-OTHER", "fw_rev": "0.9.3", "rate_hz": 250,
        }
        fake.readline = lambda: b"#CONFIG " + json.dumps(cfg).encode() + b"\n"
        fake.feed(tag(TAG_TYPE_TACTILE, 1, 2_000))
        return fake

    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/serial/by-id/oglo-left", hand="left",
        output_dir=tmp_path,
    )
    first.feed(tag(TAG_TYPE_TACTILE, 0, 1_000))
    stream.connect()
    stream.start_recording(_clock())
    first.feed(tag(TAG_TYPE_TACTILE, 1, 2_000))
    _wait(lambda: stream._frame_count >= 1)

    # From now on every open lands on a different glove claiming the path.
    sys.modules["serial"].Serial = wrong_ctor
    first.kill()
    _wait(lambda: not stream._thread.is_alive(), timeout_s=5.0)
    assert stream._frame_count == 1, "no sample from the impostor may be recorded"
    stream.disconnect()
