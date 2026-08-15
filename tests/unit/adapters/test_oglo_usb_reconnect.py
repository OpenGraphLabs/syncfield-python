"""USB link-loss auto-reconnect for the OGLO schema-6 wired adapter.

Field reality (ogpi-005, 2026-08-10): ESD/EMI from a gloved operator drops the
CDC device off the bus for ~1 s and the kernel re-enumerates it immediately.
Before reconnect existed, that one-second blip killed the reader thread and the
episode silently lost every remaining tactile sample.

The reconnect hot path skips the GET CONFIG handshake — identity is pinned by
the stable ``/dev/serial/by-id`` path, which embeds the USB serial — but performs
a bounded GET IDENT probe before proving adoption with an actual TAG packet. A
device that is back but mute (wedged ESP32-S3 CDC stack) gets one kernel USB
reset mid-window.
"""

import importlib
import json
import struct
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from syncfield.adapters.oglo.usb_packet import TAG_MAGIC, TAG_TYPE_IMU, TAG_TYPE_TACTILE
from syncfield.clock import SessionClock
from syncfield.types import HealthEventKind, SyncPoint


def tag(kind: int, seq: int, t_us: int, values=None) -> bytes:
    if kind == TAG_TYPE_IMU:
        payload = struct.pack("<6h", *(values or (0, 0, 4096, 0, 0, 0)))
    else:
        payload = struct.pack("<80H", *(values or range(80)))
    return TAG_MAGIC + bytes([kind]) + struct.pack("<HII", len(payload), seq, t_us) + payload


class _SerialError(Exception):
    """Stands in for serial.SerialException without importing pyserial."""


def _identity(
    *,
    mcu_boot_id="00112233445566778899aabbccddeeff",
    journal_boot_id="0123456789abcdef",
    journal_boot_counter=7,
    reset_reason="poweron",
):
    return {
        "ident_schema": 1,
        "mcu_boot_id": mcu_boot_id,
        "boot_count": journal_boot_counter,
        "reset_reason": reset_reason,
        "fw_rev": "0.9.14",
        "hw_rev": "RDR02_FLEX5_REV_D_TIA",
        "serial": "OGLO-TEST-L",
        "application_sha256_status": "available",
        "application_sha256": "ab" * 32,
        "uptime_ms": 1234,
        "wedge_recoveries": 0,
        "wedge_last_stall_ms": 0,
        "wedge_guard": False,
        "journal_ready": True,
        "journal_boot_counter": journal_boot_counter,
        "journal_boot_id": journal_boot_id,
    }


class _FakeSerial:
    """Streams queued TAG packets, then optionally dies like a USB unplug.

    ``stream_on_gated=True`` models a glove whose MCU rebooted during the
    outage: it stays silent until the host writes STREAM TAG ON.
    """

    def __init__(
        self,
        port,
        *,
        stream_on_gated=False,
        identity=None,
        identity_failure=None,
    ):
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
        self.identity = identity or _identity()
        self.identity_failure = identity_failure
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
        if self.writes and self.writes[-1] == b"GET IDENT\n":
            if self.identity_failure == "timeout":
                time.sleep(0.002)
                return b""
            if self.identity_failure == "unsupported":
                return b"#ERR unknown command\n"
            if self.identity_failure == "malformed":
                return b"#IDENT {not-json}\n"
            return b"#IDENT " + json.dumps(self.identity).encode() + b"\n"
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


def _usb_evidence_records(messages, module):
    prefix = module.stream._USB_EVIDENCE_PREFIX
    return [
        json.loads(message[len(prefix):])
        for message in messages
        if message.startswith(prefix)
    ]


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


def test_still_streaming_glove_records_same_identity_across_outage(
    oglo_usb, tmp_path, monkeypatch
):
    """MCU survived the blip: before/after identity is exactly joinable."""
    module, holder = oglo_usb
    first, second = _FakeSerial("p"), _FakeSerial("p")
    second.feed(tag(TAG_TYPE_TACTILE, 500, 60_000))
    holder["queue"] = [first, second]
    messages = []
    monkeypatch.setattr(
        module.stream.logger,
        "log",
        lambda _level, template, *args: messages.append(template % args),
    )

    stream, health = _connected_recording_stream(module, holder, tmp_path, first)
    first.kill()
    _wait(lambda: stream._frame_count >= 2)

    assert stream._thread.is_alive(), "reader must survive a link blip"
    assert not any(b"GET CONFIG" in w for w in second.writes), (
        "reconnect hot path must not spend time on a config handshake"
    )
    assert any(b"GET IDENT" in w for w in second.writes)
    records = _usb_evidence_records(messages, module)
    outage = next(event for event in records if event["event_type"] == "outage_observed")
    before = next(
        event
        for event in records
        if event["event_type"] == "identity_before"
        and event["outage_id"] == outage["outage_id"]
    )
    after = next(
        event
        for event in records
        if event["event_type"] == "identity_after"
        and event["outage_id"] == outage["outage_id"]
    )
    assert before["stream_id"] == after["stream_id"] == "tactile_left"
    assert before["mcu_boot_id"] == after["mcu_boot_id"]
    assert before["journal_boot_id"] == after["journal_boot_id"]
    assert after["connection_adopted"] is True
    kinds = [h.kind for h in health]
    assert HealthEventKind.WARNING in kinds
    assert any("reconnect" in h.detail.lower() for h in health)

    report = stream.stop_recording()
    stream.disconnect()
    assert report.frame_count == 2


def test_outage_records_recent_imu_peak_as_evidence_not_cause(
    oglo_usb, tmp_path, monkeypatch
):
    module, holder = oglo_usb
    first, second = _FakeSerial("p"), _FakeSerial("p")
    second.feed(tag(TAG_TYPE_TACTILE, 500, 60_000))
    holder["queue"] = [first, second]
    messages = []
    monkeypatch.setattr(
        module.stream.logger,
        "log",
        lambda _level, template, *args: messages.append(template % args),
    )

    stream, _health = _connected_recording_stream(module, holder, tmp_path, first)
    first.feed(
        tag(TAG_TYPE_IMU, 0, 2_500, (0, 0, 4096, 0, 0, 0))
        + tag(TAG_TYPE_IMU, 1, 3_000, (12_000, 0, 4096, 0, 0, 0))
    )
    _wait(lambda: len(stream._pre_outage_accel) == 2)
    first.kill()
    _wait(lambda: stream._frame_count >= 2)

    records = _usb_evidence_records(messages, module)
    outage = next(event for event in records if event["event_type"] == "outage_observed")
    imu = outage["pre_outage_imu"]
    assert imu["status"] == "observed"
    assert imu["window_ms"] == 5_000
    assert imu["sample_count"] == 2
    assert 3_000 <= imu["peak_resultant_mg"] <= 3_200
    assert imu["peak_axis_saturated"] is False
    assert imu["accelerometer_full_scale_g"] == 8
    assert imu["classification"] == "evidence_only_not_cause"
    assert not stream._pre_outage_accel

    stream.stop_recording()
    stream.disconnect()


def test_outage_marks_pre_outage_imu_unavailable_when_no_sample_arrived(
    oglo_usb, tmp_path, monkeypatch
):
    module, holder = oglo_usb
    first, second = _FakeSerial("p"), _FakeSerial("p")
    second.feed(tag(TAG_TYPE_TACTILE, 500, 60_000))
    holder["queue"] = [first, second]
    messages = []
    monkeypatch.setattr(
        module.stream.logger,
        "log",
        lambda _level, template, *args: messages.append(template % args),
    )

    stream, _health = _connected_recording_stream(module, holder, tmp_path, first)
    first.kill()
    _wait(lambda: stream._frame_count >= 2)

    records = _usb_evidence_records(messages, module)
    outage = next(event for event in records if event["event_type"] == "outage_observed")
    assert outage["pre_outage_imu"] == {
        "reason": "no_recent_imu_sample",
        "status": "unavailable",
        "window_ms": 5_000,
    }

    stream.stop_recording()
    stream.disconnect()


def test_rebooted_glove_gets_a_stream_on_nudge(oglo_usb, tmp_path, monkeypatch):
    """MCU rebooted during the outage: silent until STREAM TAG ON."""
    module, holder = oglo_usb
    first = _FakeSerial("p")
    second = _FakeSerial(
        "p",
        stream_on_gated=True,
        identity=_identity(
            mcu_boot_id="ffeeddccbbaa99887766554433221100",
            journal_boot_id="fedcba9876543210",
            journal_boot_counter=8,
            reset_reason="software",
        ),
    )
    second.feed(tag(TAG_TYPE_TACTILE, 3, 9_000))
    holder["queue"] = [first, second]
    messages = []
    monkeypatch.setattr(
        module.stream.logger,
        "log",
        lambda _level, template, *args: messages.append(template % args),
    )

    stream, _health = _connected_recording_stream(module, holder, tmp_path, first)
    first.kill()
    _wait(lambda: stream._frame_count >= 2)

    assert any(b"STREAM TAG ON" in w for w in second.writes)
    records = _usb_evidence_records(messages, module)
    outage = next(event for event in records if event["event_type"] == "outage_observed")
    after = next(
        event
        for event in records
        if event["event_type"] == "identity_after"
        and event["outage_id"] == outage["outage_id"]
    )
    assert after["mcu_boot_id"] == "ffeeddccbbaa99887766554433221100"
    assert after["journal_boot_id"] == "fedcba9876543210"
    assert after["journal_boot_counter"] == 8
    assert after["reset_reason"] == "software"
    assert after["application_sha256"] == "ab" * 32
    stream.stop_recording()
    stream.disconnect()


def test_initial_identity_probe_failure_is_explicit_and_non_gating(
    oglo_usb, tmp_path, monkeypatch
):
    module, holder = oglo_usb
    first = _FakeSerial("p", identity_failure="unsupported")
    first.feed(tag(TAG_TYPE_TACTILE, 0, 1_000))
    holder["queue"] = [first]
    messages = []
    monkeypatch.setattr(
        module.stream.logger,
        "log",
        lambda _level, template, *args: messages.append(template % args),
    )

    stream = module.OgloTactileStream(
        "tactile_left",
        serial_port="/dev/serial/by-id/oglo-left",
        hand="left",
        output_dir=tmp_path,
    )
    stream.connect()
    records = _usb_evidence_records(messages, module)
    failure = next(
        event for event in records if event["event_type"] == "identity_probe_failed"
    )
    assert failure["phase"] == "before"
    assert failure["failure_class"] == "unsupported"
    assert failure["outage_id"]
    assert stream._thread.is_alive()
    stream.disconnect()


def test_reconnect_identity_failure_keeps_recovery_and_outage_join(
    oglo_usb, tmp_path, monkeypatch
):
    module, holder = oglo_usb
    first = _FakeSerial("p")
    second = _FakeSerial("p", identity_failure="malformed")
    second.feed(tag(TAG_TYPE_TACTILE, 9, 9_000))
    holder["queue"] = [first, second]
    messages = []
    monkeypatch.setattr(
        module.stream.logger,
        "log",
        lambda _level, template, *args: messages.append(template % args),
    )

    stream, _health = _connected_recording_stream(module, holder, tmp_path, first)
    first.kill()
    _wait(lambda: stream._frame_count >= 2)
    records = _usb_evidence_records(messages, module)
    outage = next(event for event in records if event["event_type"] == "outage_observed")
    failure = next(
        event
        for event in records
        if event["event_type"] == "identity_probe_failed"
        and event["phase"] == "after"
    )
    assert failure["outage_id"] == outage["outage_id"]
    assert failure["failure_class"] == "malformed"
    assert failure["connection_adopted"] is True
    assert any(
        event["event_type"] == "recovery_result"
        and event["outcome"] == "recovered"
        for event in records
    )
    stream.stop_recording()
    stream.disconnect()


def test_identity_parser_rejects_a_hash_status_that_claims_missing_bytes(oglo_usb):
    module, _holder = oglo_usb
    identity = _identity()
    identity.pop("application_sha256")

    with pytest.raises(module.stream.OgloProtocolError, match="application_sha256"):
        module.stream._parse_usb_identity(json.dumps(identity).encode())


def test_identity_parser_preserves_explicitly_unavailable_application_hash(oglo_usb):
    module, _holder = oglo_usb
    identity = _identity()
    identity.pop("application_sha256")
    identity["application_sha256_status"] = "unavailable"

    parsed = module.stream._parse_usb_identity(json.dumps(identity).encode())

    assert parsed["application_sha256_status"] == "unavailable"
    assert "application_sha256" not in parsed


def test_identity_parser_accepts_the_hand_flashed_0913_legacy_shape(oglo_usb):
    module, _holder = oglo_usb
    identity = _identity()
    identity.pop("ident_schema")
    identity.pop("application_sha256_status")
    identity.pop("application_sha256")
    identity.pop("journal_ready")
    identity.pop("journal_boot_counter")
    identity.pop("journal_boot_id")
    identity["fw_rev"] = "0.9.13"

    parsed = module.stream._parse_usb_identity(json.dumps(identity).encode())

    assert parsed["ident_schema"] == 0
    assert parsed["application_sha256_status"] == "unavailable"
    assert parsed["journal_status"] == "unavailable"


def test_identity_parser_matches_the_shared_v1_golden_vector(oglo_usb):
    module, _holder = oglo_usb
    fixture = (
        Path(__file__).with_name("oglo") / "ident_contract_v1.json"
    ).read_bytes()

    parsed = module.stream._parse_usb_identity(fixture)

    assert parsed["ident_schema"] == 1
    assert parsed["mcu_boot_id"] == "00112233445566778899aabbccddeeff"
    assert parsed["application_sha256_status"] == "available"
    assert parsed["journal_status"] == "available"


@pytest.mark.parametrize(
    "mcu_boot_id",
    ("mcu-boot-1", "A" * 32, "a" * 31, "a" * 33),
)
def test_identity_parser_rejects_noncanonical_mcu_boot_id(
    oglo_usb, mcu_boot_id
):
    module, _holder = oglo_usb
    identity = _identity(mcu_boot_id=mcu_boot_id)

    with pytest.raises(module.stream.OgloProtocolError, match="mcu_boot_id"):
        module.stream._parse_usb_identity(json.dumps(identity).encode())


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


def test_present_but_mute_device_escalates_to_one_usb_reset(
    oglo_usb, tmp_path, monkeypatch
):
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
    messages = []
    monkeypatch.setattr(
        module.stream.logger,
        "log",
        lambda _level, template, *args: messages.append(template % args),
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
    records = _usb_evidence_records(messages, module)
    event_types = [event["event_type"] for event in records]
    assert "usb_reset_requested" in event_types
    assert "usb_reset_result" in event_types
    assert "recovery_result" in event_types
    assert not any(event_type.startswith("reader_") for event_type in event_types)
    incident_records = [event for event in records if "outage_id" in event]
    assert len({event["outage_id"] for event in incident_records}) == 1
    assert all("parent_event_id" not in event for event in records)
    recovery = next(
        event for event in records if event["event_type"] == "recovery_result"
    )
    assert recovery["reconnect_summary"]["attempts"] >= 1
    assert recovery["reconnect_summary"]["adoption_failures"] >= 1
    assert recovery["reconnect_summary"]["reset_attempted"] is True
    assert recovery["reconnect_summary"]["reset_outcome"] == "succeeded"
    assert "reconnect_spans" not in recovery
    stream.disconnect()


def test_usb_flight_log_is_bounded_metadata_only(oglo_usb, tmp_path, monkeypatch):
    module, _holder = oglo_usb
    stream = module.OgloTactileStream(
        "tactile_left",
        serial_port="/dev/serial/by-id/oglo-left",
        hand="left",
        output_dir=tmp_path,
    )

    for index in range(70):
        stream._record_usb_read(
            started_ns=index,
            byte_count=index,
            diagnostic="valid_frame",
        )

    assert len(stream._usb_read_history) == 16
    assert all(isinstance(entry, tuple) for entry in stream._usb_read_history)
    projection = stream._usb_read_history_projection()
    assert len(projection) == 16
    assert projection[0]["byte_count"] == 54
    assert set(projection[0]) == {"duration_us", "byte_count", "classification"}

    messages = []
    monkeypatch.setattr(
        module.stream.logger,
        "log",
        lambda _level, template, *args: messages.append(template % args),
    )
    stream._log_usb_evidence(
        "outage_observed",
        outage_id="outage-test",
        read_spans=projection,
    )

    records = _usb_evidence_records(messages, module)
    assert records[-1]["event_type"] == "outage_observed"
    assert records[-1]["outage_id"] == "outage-test"
    assert records[-1]["schema_version"] == "oglo.usb_evidence.v1"
    assert records[-1]["source_monotonic_ns"] > 0
    assert records[-1]["source_realtime_ns"] > 0
    assert "usb_serial" in records[-1]
    assert "usb_physical_path" in records[-1]
    assert "usb_controller" in records[-1]
    for redundant in (
        "event_id",
        "origin_seq",
        "host_boot_id",
        "invocation_id",
        "parent_event_id",
        "raw_payload",
    ):
        assert redundant not in records[-1]


def test_usb_read_history_keeps_only_real_errno(oglo_usb, tmp_path):
    module, _holder = oglo_usb
    stream = module.OgloTactileStream(
        "tactile_left",
        serial_port="/dev/serial/by-id/oglo-left",
        hand="left",
        output_dir=tmp_path,
    )
    stream._record_usb_read(
        started_ns=1,
        byte_count=0,
        diagnostic="read_exception",
        exception=OSError(5, "x" * 800),
    )
    stream._record_usb_read(
        started_ns=2,
        byte_count=0,
        diagnostic="zero_byte_read",
    )

    projection = stream._usb_read_history_projection()
    assert projection[0]["errno"] == 5
    assert "errno" not in projection[1]
    assert all("exception_message" not in item for item in projection)
