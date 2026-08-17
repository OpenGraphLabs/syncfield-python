"""Host keepalive contract for firmware 0.9.16 USB wedge recovery."""

from __future__ import annotations

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
from syncfield.types import SyncPoint


def _tag(seq: int, device_us: int) -> bytes:
    payload = struct.pack("<80H", *range(80))
    return (
        TAG_MAGIC
        + bytes([TAG_TYPE_TACTILE])
        + struct.pack("<HII", len(payload), seq, device_us)
        + payload
    )


class _SerialError(Exception):
    pass


class _FakeSerial:
    def __init__(
        self,
        *,
        fw_rev: str = "0.9.16",
        link_ping: object = True,
        initial: bytes | None = None,
        write_delay_s: float = 0.0,
    ) -> None:
        self.port = None
        self.baudrate = 115200
        self.timeout = 0.1
        self.write_timeout = None
        self.dtr = True
        self.rts = True
        self.closed = False
        self.fw_rev = fw_rev
        self.link_ping = link_ping
        self.write_delay_s = write_delay_s
        self.writes: list[bytes] = []
        self.write_times: list[tuple[bytes, float]] = []
        self.concurrent_write = False
        self.ping_failure: str | None = None
        self.ping_attempts = 0
        self._active_writes = 0
        self._write_guard = threading.Lock()
        self._read_guard = threading.Lock()
        self._to_read = _tag(0, 1_000) if initial is None else initial
        self._dead = False

    def open(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        with self._write_guard:
            if self._active_writes:
                self.concurrent_write = True
            self._active_writes += 1
        try:
            if self.write_delay_s:
                time.sleep(self.write_delay_s)
            value = bytes(data)
            if value == b"LINK PING\n":
                self.ping_attempts += 1
                failure, self.ping_failure = self.ping_failure, None
                if failure == "raise":
                    raise _SerialError("injected ping write failure")
                if failure == "short":
                    partial = value[:-1]
                    self.writes.append(partial)
                    self.write_times.append((partial, time.monotonic()))
                    return len(partial)
            self.writes.append(value)
            self.write_times.append((value, time.monotonic()))
            return len(value)
        finally:
            with self._write_guard:
                self._active_writes -= 1

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        config = {
            "device": "oglo",
            "schema_ver": 6,
            "side": "right",
            "serial": "OGLO-LINK-PING-TEST",
            "fw_rev": self.fw_rev,
            "rate_hz": 250,
            "values_per_sample": 80,
            "sample_shape": [5, 4, 4],
            "link_ping": self.link_ping,
        }
        return b"#CONFIG " + json.dumps(config).encode() + b"\n"

    def read(self, size: int) -> bytes:
        with self._read_guard:
            if self._dead:
                raise _SerialError("USB handle disappeared")
            if self._to_read:
                value, self._to_read = self._to_read[:size], self._to_read[size:]
                return value
        time.sleep(0.002)
        return b""

    def feed(self, data: bytes) -> None:
        with self._read_guard:
            self._to_read += data

    def kill(self) -> None:
        with self._read_guard:
            self._dead = True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def oglo_link_ping(monkeypatch):
    monkeypatch.setitem(sys.modules, "bleak", MagicMock())
    holder: dict[str, list[_FakeSerial]] = {"queue": [], "created": []}

    def ctor():
        fake = holder["queue"].pop(0)
        holder["created"].append(fake)
        return fake

    monkeypatch.setitem(
        sys.modules,
        "serial",
        SimpleNamespace(Serial=ctor, SerialException=_SerialError),
    )
    for name in ("syncfield.adapters.oglo", "syncfield.adapters.oglo.stream"):
        sys.modules.pop(name, None)
    module = importlib.import_module("syncfield.adapters.oglo")
    stream_module = importlib.import_module("syncfield.adapters.oglo.stream")
    monkeypatch.setattr(stream_module, "_LINK_PING_INTERVAL_S", 0.02)
    yield module, holder
    for name in ("syncfield.adapters.oglo", "syncfield.adapters.oglo.stream"):
        sys.modules.pop(name, None)


def _connect(module, holder, fake: _FakeSerial):
    holder["queue"] = [fake]
    stream = module.OgloTactileStream(
        "tactile_right", serial_port="/dev/serial/by-id/oglo-right", hand="right"
    )
    stream.connect()
    return stream


def _wait(predicate, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.002)
    assert predicate(), "condition not met in time"


def _pings(fake: _FakeSerial) -> list[bytes]:
    return [write for write in fake.writes if write == b"LINK PING\n"]


def test_0_9_15_never_receives_link_ping_even_if_capability_is_present(
    oglo_link_ping,
):
    module, holder = oglo_link_ping
    fake = _FakeSerial(fw_rev="0.9.15", link_ping=True)
    stream = _connect(module, holder, fake)
    time.sleep(0.07)
    assert _pings(fake) == []
    assert stream._link_ping_thread is None
    stream.disconnect()


def test_0_9_16_without_explicit_capability_never_receives_link_ping(
    oglo_link_ping,
):
    module, holder = oglo_link_ping
    fake = _FakeSerial(fw_rev="0.9.16", link_ping=False)
    stream = _connect(module, holder, fake)
    time.sleep(0.07)
    assert _pings(fake) == []
    assert stream._link_ping_thread is None
    stream.disconnect()


def test_0_9_16_with_capability_receives_periodic_link_ping(oglo_link_ping):
    module, holder = oglo_link_ping
    fake = _FakeSerial()
    stream = _connect(module, holder, fake)
    _wait(lambda: len(_pings(fake)) >= 3)

    times = [at for write, at in fake.write_times if write == b"LINK PING\n"][:3]
    assert times[2] - times[1] <= 0.08
    assert fake.write_timeout == 0.5
    assert stream._link_ping_generation == stream._usb_connection_generation == 1
    stream.disconnect()


def test_reconnect_sends_fresh_ping_for_new_usb_generation(oglo_link_ping):
    module, holder = oglo_link_ping
    first = _FakeSerial(initial=_tag(0, 1_000))
    second = _FakeSerial(initial=_tag(10, 20_000))
    holder["queue"] = [first, second]
    stream = module.OgloTactileStream(
        "tactile_right", serial_port="/dev/serial/by-id/oglo-right", hand="right"
    )
    stream.connect()
    _wait(lambda: len(_pings(first)) >= 1)
    first_worker = stream._link_ping_thread

    first.kill()
    _wait(lambda: len(_pings(second)) >= 1)

    assert first_worker is not None and not first_worker.is_alive()
    assert stream._link_ping_thread is not first_worker
    assert stream._usb_connection_generation == 2
    assert stream._link_ping_generation == 2
    assert b"GET CONFIG\n" not in second.writes
    assert second.writes.index(b"STREAM TAG ON\n") < second.writes.index(b"LINK PING\n")
    stream.disconnect()


@pytest.mark.parametrize("failure", ["raise", "short"])
def test_ping_write_failure_reconnects_without_retrying_poisoned_generation(
    oglo_link_ping, failure
):
    module, holder = oglo_link_ping
    first = _FakeSerial(initial=_tag(0, 1_000))
    second = _FakeSerial(initial=_tag(10, 20_000))
    holder["queue"] = [first, second]
    stream = module.OgloTactileStream(
        "tactile_right", serial_port="/dev/serial/by-id/oglo-right", hand="right"
    )
    stream.connect()
    _wait(lambda: len(_pings(first)) >= 1)
    attempts_before_failure = first.ping_attempts

    first.ping_failure = failure
    _wait(lambda: len(_pings(second)) >= 1)

    assert first.ping_attempts == attempts_before_failure + 1
    assert stream._usb_connection_generation == 2
    assert stream._link_ping_generation == 2
    if failure == "short":
        assert first.writes.count(b"LINK PING") == 1
        partial_index = first.writes.index(b"LINK PING")
        assert b"LINK PING\n" not in first.writes[partial_index + 1 :]
    stream.disconnect()


def test_disconnect_after_partial_ping_closes_without_appending_stop(
    oglo_link_ping,
):
    module, holder = oglo_link_ping
    fake = _FakeSerial()
    stream = _connect(module, holder, fake)
    _wait(lambda: len(_pings(fake)) >= 1)

    fake.ping_failure = "short"
    _wait(lambda: b"LINK PING" in fake.writes)
    partial_index = fake.writes.index(b"LINK PING")
    stream.disconnect()

    assert fake.writes[partial_index + 1 :] == []
    assert fake.closed is True


def test_disconnect_joins_worker_and_stops_all_future_pings(oglo_link_ping):
    module, holder = oglo_link_ping
    fake = _FakeSerial()
    stream = _connect(module, holder, fake)
    _wait(lambda: len(_pings(fake)) >= 2)
    worker = stream._link_ping_thread

    stream.disconnect()
    count_at_close = len(_pings(fake))
    time.sleep(0.07)

    assert worker is not None and not worker.is_alive()
    assert stream._link_ping_thread is None
    assert stream._thread is None
    assert len(_pings(fake)) == count_at_close


def test_disconnect_never_forgets_a_reader_that_is_still_alive(oglo_link_ping):
    module, _holder = oglo_link_ping
    stream = module.OgloTactileStream(
        "tactile_right", serial_port="/dev/serial/by-id/oglo-right", hand="right"
    )

    class StuckReader:
        def __init__(self):
            self.join_timeouts = []

        def join(self, timeout=None):
            self.join_timeouts.append(timeout)

        def is_alive(self):
            return True

    stuck = StuckReader()
    stream._thread = stuck
    stream.disconnect()

    assert stuck.join_timeouts == [3.0]
    assert stream._thread is stuck


def test_two_gloves_have_independent_keepalive_lifecycles(oglo_link_ping):
    module, holder = oglo_link_ping
    first = _FakeSerial()
    second = _FakeSerial()
    first_stream = _connect(module, holder, first)
    second_stream = _connect(module, holder, second)
    _wait(lambda: len(_pings(first)) >= 2 and len(_pings(second)) >= 2)

    first_stream.disconnect()
    first_count = len(_pings(first))
    second_count = len(_pings(second))
    _wait(lambda: len(_pings(second)) > second_count)

    assert len(_pings(first)) == first_count
    assert second_stream._link_ping_thread is not None
    assert second_stream._link_ping_thread.is_alive()
    second_stream.disconnect()


def test_keepalive_spans_recording_and_idle_preview(oglo_link_ping, tmp_path):
    module, holder = oglo_link_ping
    fake = _FakeSerial()
    holder["queue"] = [fake]
    stream = module.OgloTactileStream(
        "tactile_right",
        serial_port="/dev/serial/by-id/oglo-right",
        hand="right",
        output_dir=tmp_path,
    )
    stream.connect()
    stream.start_recording(
        SessionClock(
            sync_point=SyncPoint.create_now("pi-test"),
            recording_armed_ns=time.monotonic_ns(),
        )
    )
    fake.feed(_tag(1, 2_000))
    _wait(lambda: stream._frame_count >= 1 and len(_pings(fake)) >= 2)

    report = stream.stop_recording()
    pings_after_recording = len(_pings(fake))
    _wait(lambda: len(_pings(fake)) > pings_after_recording)

    assert report.status == "completed"
    assert stream._recording is False
    stream.disconnect()


def test_shared_write_transaction_blocks_ping_from_raw_transfer(oglo_link_ping):
    module, holder = oglo_link_ping
    fake = _FakeSerial(write_delay_s=0.005)
    stream = _connect(module, holder, fake)
    _wait(lambda: len(_pings(fake)) >= 1)

    raw_exchange = [b"FW BEGIN 4\n", b"\x00\x01\x02\x03", b"FW END\n"]
    with stream._serial_write_transaction():
        for chunk in raw_exchange:
            fake.write(chunk)
            time.sleep(0.025)

    _wait(lambda: len(_pings(fake)) >= 2)
    begin = fake.writes.index(raw_exchange[0])
    assert fake.writes[begin : begin + len(raw_exchange)] == raw_exchange
    assert fake.concurrent_write is False
    stream.disconnect()
