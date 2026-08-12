"""A silent-but-open OGLO link must be detected, not waited on forever.

Field incident (ogpi-007, 2026-08-12, recording 4270711e): the right glove's
USB link dropped 24 minutes into a 60-minute session. Auto-reconnect logged
"USB link reconnected after 8443 ms" — and not one further sample ever
arrived. The reader thread stayed alive spinning on empty reads, so the
host's protective stop (which keys off thread death) never fired, and the rig
recorded for another 36 minutes with a dead right hand: 486 MB of right-hand
taxels in segment 0 against 605 MB of left, and four 0-byte right-hand files
in segment 1.

Nothing in the loop measured "am I still receiving data?". These tests pin
that: after the stream has gone ready, sustained silence is a dead link, and
it must either recover or kill the thread so the host can stop the recording.
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


def tag(seq: int, t_us: int) -> bytes:
    payload = struct.pack("<80H", *range(80))
    return (
        TAG_MAGIC
        + bytes([TAG_TYPE_TACTILE])
        + struct.pack("<HII", len(payload), seq, t_us)
        + payload
    )


class _SerialError(Exception):
    pass


class _FakeSerial:
    """A port that streams, then goes quiet without ever erroring."""

    def __init__(self, port):
        self.port, self.writes, self.closed = port, [], False
        self.dtr = self.rts = True
        self.dsrdtr = self.rtscts = False
        self.timeout = 0.1
        self._lock = threading.Lock()
        self._to_read = b""

    def open(self):
        pass

    def feed(self, data):
        with self._lock:
            self._to_read += data

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def readline(self):
        import json

        cfg = {
            "device": "oglo", "schema_ver": 6, "side": "right",
            "serial": "OGLO-TEST-R", "fw_rev": "0.9.3", "rate_hz": 250,
        }
        return b"#CONFIG " + json.dumps(cfg).encode() + b"\n"

    def read(self, n):
        with self._lock:
            if self._to_read:
                out, self._to_read = self._to_read[:n], self._to_read[n:]
                return out
        time.sleep(0.002)
        return b""  # open, healthy-looking, and saying nothing

    def close(self):
        self.closed = True


@pytest.fixture
def oglo_usb(monkeypatch):
    monkeypatch.setitem(sys.modules, "bleak", MagicMock())
    holder = {"queue": [], "created": []}

    def ctor(port=None, baudrate=115200, timeout=0.1, dsrdtr=False, rtscts=False):
        fake = holder["queue"].pop(0) if holder["queue"] else _FakeSerial(port)
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


def _wait(predicate, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate(), "condition not met in time"


def test_silence_after_ready_triggers_reconnect(oglo_usb, tmp_path, monkeypatch):
    """The ogpi-007 shape exactly: the port stays open and simply stops
    delivering. The reader must notice and re-establish the link."""
    module, holder = oglo_usb
    monkeypatch.setattr(module.stream, "_STREAM_SILENCE_TIMEOUT_S", 0.4)

    first, second = _FakeSerial("p"), _FakeSerial("p")
    first.feed(tag(0, 1_000))
    second.feed(tag(5, 20_000))
    holder["queue"] = [first, second]

    stream = module.OgloTactileStream(
        "tactile_right", serial_port="/dev/serial/by-id/oglo-right", hand="right",
        output_dir=tmp_path,
    )
    health = []
    stream.on_health(health.append)
    stream.connect()
    stream.start_recording(_clock())

    # first never sends again; the watchdog must act on silence alone.
    _wait(lambda: stream._frame_count >= 1)
    second.feed(tag(6, 24_000))  # the loop must now be reading the new handle
    _wait(lambda: stream._frame_count >= 2)

    assert stream._thread.is_alive()
    assert any("no TAG packet" in h.detail for h in health), (
        "the outage must be recorded, not silently repaired"
    )
    stream.stop_recording()
    stream.disconnect()


def test_unrecoverable_silence_kills_the_thread(oglo_usb, tmp_path, monkeypatch):
    """When the link cannot be revived the thread must exit. Thread death is
    the host's protective-stop trigger — staying alive is what let ogpi-007
    record 36 minutes of nothing."""
    module, holder = oglo_usb
    monkeypatch.setattr(module.stream, "_STREAM_SILENCE_TIMEOUT_S", 0.4)
    monkeypatch.setattr(module.stream, "_RECONNECT_WINDOW_S", 0.6)
    monkeypatch.setattr(module.stream, "_RECONNECT_RETRY_INTERVAL_S", 0.05)
    monkeypatch.setattr(module.stream, "_RECONNECT_ATTEMPT_DEADLINE_S", 0.15)

    first = _FakeSerial("p")
    first.feed(tag(0, 1_000))
    holder["queue"] = [first]  # every later open yields a fake that never speaks

    stream = module.OgloTactileStream(
        "tactile_right", serial_port="/dev/serial/by-id/oglo-right", hand="right",
        output_dir=tmp_path,
    )
    health = []
    stream.on_health(health.append)
    stream.connect()
    stream.start_recording(_clock())

    _wait(lambda: not stream._thread.is_alive())

    assert any(h.kind is HealthEventKind.ERROR for h in health), (
        "an unrecoverable silent link must fail loud"
    )
    stream.disconnect()


def test_reconnect_always_reasserts_stream_on(oglo_usb, tmp_path, monkeypatch):
    """A glove that re-enumerated boots idle. The previous code only nudged it
    when the read buffer was empty, so any stray byte could leave the device
    parked forever while adoption still "succeeded"."""
    module, holder = oglo_usb
    monkeypatch.setattr(module.stream, "_STREAM_SILENCE_TIMEOUT_S", 0.4)

    first, second = _FakeSerial("p"), _FakeSerial("p")
    first.feed(tag(0, 1_000))
    second.feed(tag(9, 30_000))
    holder["queue"] = [first, second]

    stream = module.OgloTactileStream(
        "tactile_right", serial_port="/dev/serial/by-id/oglo-right", hand="right",
        output_dir=tmp_path,
    )
    stream.connect()
    stream.start_recording(_clock())
    _wait(lambda: stream._frame_count >= 1)

    assert any(b"STREAM TAG ON" in w for w in second.writes), (
        "the replacement handle must be told to stream, unconditionally"
    )
    stream.stop_recording()
    stream.disconnect()


def test_healthy_stream_is_never_interrupted(oglo_usb, tmp_path, monkeypatch):
    """The watchdog must not touch a stream that keeps delivering."""
    module, holder = oglo_usb
    monkeypatch.setattr(module.stream, "_STREAM_SILENCE_TIMEOUT_S", 0.4)

    first = _FakeSerial("p")
    first.feed(tag(0, 1_000))
    holder["queue"] = [first]

    stream = module.OgloTactileStream(
        "tactile_right", serial_port="/dev/serial/by-id/oglo-right", hand="right",
        output_dir=tmp_path,
    )
    health = []
    stream.on_health(health.append)
    stream.connect()
    stream.start_recording(_clock())

    for seq in range(1, 12):  # keep feeding across several watchdog periods
        first.feed(tag(seq, 1_000 + seq * 4_000))
        time.sleep(0.1)

    assert stream._thread.is_alive()
    assert not any("no TAG packet" in (h.detail or "") for h in health)
    assert len(holder["created"]) == 1, "no reconnect may be attempted"
    stream.stop_recording()
    stream.disconnect()
