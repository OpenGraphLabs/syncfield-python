"""OGLO 0.9.3+ schema-6 USB transport driven by fake serial."""

import importlib
import json
import struct
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from syncfield.adapters.oglo.usb_packet import (
    TAG_MAGIC,
    TAG_V2_MAGIC,
    TAG_TYPE_IMU,
    TAG_TYPE_MAG,
    TAG_TYPE_TACTILE,
    parse_usb_packet,
)
from syncfield.clock import SessionClock
from syncfield.types import SyncPoint


def tag(kind: int, seq: int, t_us: int, values=None) -> bytes:
    counts = {TAG_TYPE_TACTILE: 80, TAG_TYPE_IMU: 6, TAG_TYPE_MAG: 3}
    formats = {TAG_TYPE_TACTILE: "<80H", TAG_TYPE_IMU: "<6h", TAG_TYPE_MAG: "<3h"}
    values = values if values is not None else list(range(counts[kind]))
    payload = struct.pack(formats[kind], *values)
    return TAG_MAGIC + bytes([kind]) + struct.pack("<HII", len(payload), seq, t_us) + payload


def tag_v2(kind: int, seq: int, t_us: int, values=None) -> bytes:
    counts = {TAG_TYPE_TACTILE: 80, TAG_TYPE_IMU: 6, TAG_TYPE_MAG: 3}
    formats = {TAG_TYPE_TACTILE: "<80H", TAG_TYPE_IMU: "<6h", TAG_TYPE_MAG: "<3h"}
    values = values if values is not None else list(range(counts[kind]))
    if kind == TAG_TYPE_TACTILE:
        payload = bytearray()
        for index in range(0, len(values), 2):
            first = values[index] & 0x0FFF
            second = values[index + 1] & 0x0FFF
            payload.extend(
                (
                    first >> 4,
                    ((first & 0x0F) << 4) | (second >> 8),
                    second & 0xFF,
                )
            )
        payload = bytes(payload)
    else:
        payload = struct.pack(formats[kind], *values)
    return TAG_V2_MAGIC + bytes([kind]) + struct.pack("<HIQ", len(payload), seq, t_us) + payload


class _FakeSerial:
    def __init__(
        self, port=None, baudrate=115200, timeout=0.1, dsrdtr=False, rtscts=False
    ):
        self.port, self.writes, self.closed = port, [], False
        self.baudrate = baudrate
        self.timeout = timeout
        self.dsrdtr = dsrdtr
        self.rtscts = rtscts
        self.dtr = True
        self.rts = True
        self.opened_with = None
        self._lock = threading.Lock()
        self._to_read = tag(TAG_TYPE_TACTILE, 0, 1000)

    def open(self):
        self.opened_with = (self.dtr, self.rts, self.dsrdtr, self.rtscts)

    def feed(self, data):
        with self._lock:
            self._to_read += data

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(bytes(data)); return len(data)

    def flush(self):
        pass

    def readline(self):
        cfg = {"device": "oglo", "schema_ver": 6, "side": "left" if self.port.endswith("1") else "right",
               "serial": "OGLO-TEST", "fw_rev": "0.9.3", "rate_hz": 250,
               "values_per_sample": 80, "sample_shape": [5, 4, 4]}
        return b"#CONFIG " + json.dumps(cfg).encode() + b"\n"

    def read(self, n):
        with self._lock:
            if self._to_read:
                out, self._to_read = self._to_read[:n], self._to_read[n:]
                return out
        time.sleep(0.005); return b""

    def close(self):
        self.closed = True


@pytest.fixture
def oglo_usb(monkeypatch):
    monkeypatch.setitem(sys.modules, "bleak", MagicMock())
    holder = {}
    def ctor(
        port=None, baudrate=115200, timeout=0.1, dsrdtr=False, rtscts=False
    ):
        holder["serial"] = _FakeSerial(
            port, baudrate, timeout, dsrdtr, rtscts
        ); return holder["serial"]
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=ctor))
    for name in ("syncfield.adapters.oglo", "syncfield.adapters.oglo.stream"):
        sys.modules.pop(name, None)
    module = importlib.import_module("syncfield.adapters.oglo")
    yield module, holder
    for name in ("syncfield.adapters.oglo", "syncfield.adapters.oglo.stream"):
        sys.modules.pop(name, None)


def test_requires_wired_transport(oglo_usb):
    module, _ = oglo_usb
    with pytest.raises(ValueError, match="USB-wired"):
        module.OgloTactileStream("x")


def test_connect_validates_config_and_enables_tag(oglo_usb):
    module, holder = oglo_usb
    samples = []
    stream = module.OgloTactileStream("tactile_right", serial_port="/dev/ttyACM0", hand="right")
    stream.on_sample(samples.append)
    stream.connect()
    fake = holder["serial"]
    assert fake.opened_with == (True, False, False, False)
    assert fake.rtscts is False
    assert stream._manifest.schema_ver == 6
    assert any(b"GET CONFIG" in write for write in fake.writes)
    assert any(b"STREAM TAG ON" in write for write in fake.writes)
    assert samples
    stream.disconnect()
    assert any(b"STREAM TAG OFF" in write for write in fake.writes)
    assert fake.closed


def test_tag_v2_is_negotiated_from_config_and_preserves_u64_time(oglo_usb, monkeypatch):
    module, holder = oglo_usb
    boot_id = "0123456789abcdef0123456789abcdef"
    original_init = _FakeSerial.__init__

    def v2_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._to_read = (
            f"#STREAM TAG2 on boot_id={boot_id}\r\n".encode()
            + tag_v2(TAG_TYPE_TACTILE, 0, (1 << 32) + 123)
        )

    def v2_config(self):
        cfg = {
            "device": "oglo", "schema_ver": 6, "side": "right",
            "serial": "OGLO-TEST", "fw_rev": "0.9.16", "rate_hz": 250,
            "values_per_sample": 80, "sample_shape": [5, 4, 4],
            "tag_ver_max": 2, "boot_id": boot_id, "link_ping": True,
        }
        return b"#CONFIG " + json.dumps(cfg).encode() + b"\n"

    monkeypatch.setattr(_FakeSerial, "__init__", v2_init)
    monkeypatch.setattr(_FakeSerial, "readline", v2_config)
    samples = []
    stream = module.OgloTactileStream(
        "tactile_right", serial_port="/dev/ttyACM0", hand="right"
    )
    stream.on_sample(samples.append)
    stream.connect()
    fake = holder["serial"]
    assert any(write == b"STREAM TAG2 ON\n" for write in fake.writes)
    assert stream._tag_version == 2
    assert stream._stream_boot_id == boot_id
    assert samples[0].device_ns == ((1 << 32) + 123) * 1_000
    assert tuple(samples[0].channels.values()) == tuple(range(80))
    deadline = time.monotonic() + 1.0
    while b"LINK PING\n" not in fake.writes and time.monotonic() < deadline:
        time.sleep(0.005)
    assert fake.writes.index(b"STREAM TAG2 ON\n") < fake.writes.index(b"LINK PING\n")
    stream.disconnect()
    assert any(write == b"STREAM TAG2 OFF\n" for write in fake.writes)


def test_tag_v1_time_unwraps_at_71_minute_rollover(oglo_usb):
    module, _ = oglo_usb
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/ttyACM1", hand="left"
    )
    stream._channel_labels = tuple(f"t{i}" for i in range(80))
    samples = []
    stream.on_sample(samples.append)
    stream._handle_usb_packet(
        parse_usb_packet(tag(TAG_TYPE_TACTILE, 10, 0xFFFFFFF0))
    )
    stream._handle_usb_packet(
        parse_usb_packet(tag(TAG_TYPE_TACTILE, 11, 0x00000020))
    )
    assert samples[1].device_ns - samples[0].device_ns == 48_000


def test_usb_read_batch_uses_device_spacing_and_rejects_late_backlog(oglo_usb):
    module, _ = oglo_usb
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/ttyACM1", hand="left"
    )
    stream._set_tag_version(2)
    stream._channel_labels = tuple(f"t{i}" for i in range(80))
    samples = []
    stream.on_sample(samples.append)

    first = tuple(
        parse_usb_packet(tag_v2(TAG_TYPE_TACTILE, seq, t_us))
        for seq, t_us in ((0, 1_000), (1, 5_000), (2, 9_000))
    )
    stream._handle_usb_packets(first, receive_ns=100_000_000)
    second = tuple(
        parse_usb_packet(tag_v2(TAG_TYPE_TACTILE, seq, t_us))
        for seq, t_us in ((3, 13_000), (4, 17_000), (5, 21_000))
    )
    # This batch sat in the kernel/tty queue for 188 ms.  Parser/receipt time
    # must not move the already-known device timeline to 292..300 ms.
    stream._handle_usb_packets(second, receive_ns=300_000_000)

    assert [sample.capture_ns for sample in samples] == [
        92_000_000,
        96_000_000,
        100_000_000,
        104_000_000,
        108_000_000,
        112_000_000,
    ]
    assert all(sample.uncertainty_ns == 100_000_000 for sample in samples)


def test_tag_v1_clock_reset_is_new_preview_epoch_but_fails_recording(oglo_usb):
    module, _ = oglo_usb
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/ttyACM1", hand="left"
    )
    stream._channel_labels = tuple(f"t{i}" for i in range(80))
    stream._handle_usb_packet(
        parse_usb_packet(tag(TAG_TYPE_TACTILE, 10, 1_000_000))
    )
    # Preview may safely establish a fresh epoch after an MCU restart.
    stream._next_expected_seq = {}
    stream._handle_usb_packet(
        parse_usb_packet(tag(TAG_TYPE_TACTILE, 0, 100))
    )
    stream._recording = True
    stream._next_expected_seq = {}
    with pytest.raises(Exception, match="moved backward"):
        stream._handle_usb_packet(
            parse_usb_packet(tag(TAG_TYPE_TACTILE, 1, 50))
        )


def test_changed_tag_v2_boot_id_stops_an_active_recording(oglo_usb):
    module, _ = oglo_usb
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/ttyACM1", hand="left"
    )
    stream._stream_boot_id = "0" * 32
    stream._recording = True
    with pytest.raises(Exception, match="rebooted during recording"):
        stream._accept_tag2_boot_id("1" * 32)


def test_tag_v2_config_ack_boot_id_mismatch_fails_connect_closed(
    oglo_usb, monkeypatch
):
    module, holder = oglo_usb
    config_boot_id = "0" * 32
    ack_boot_id = "1" * 32
    original_init = _FakeSerial.__init__

    def v2_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._to_read = (
            f"#STREAM TAG2 on boot_id={ack_boot_id}\r\n".encode()
            + tag_v2(TAG_TYPE_TACTILE, 0, 1)
        )

    def v2_config(self):
        cfg = {
            "device": "oglo",
            "schema_ver": 6,
            "side": "right",
            "serial": "OGLO-TEST",
            "fw_rev": "0.9.13",
            "rate_hz": 250,
            "values_per_sample": 80,
            "sample_shape": [5, 4, 4],
            "tag_ver_max": 2,
            "boot_id": config_boot_id,
        }
        return b"#CONFIG " + json.dumps(cfg).encode() + b"\n"

    monkeypatch.setattr(_FakeSerial, "__init__", v2_init)
    monkeypatch.setattr(_FakeSerial, "readline", v2_config)
    stream = module.OgloTactileStream(
        "tactile_right", serial_port="/dev/ttyACM0", hand="right"
    )

    with pytest.raises(Exception, match="CONFIG/ACK boot_id mismatch"):
        stream.connect()

    candidate = holder["serial"]
    assert candidate.closed
    assert b"STREAM TAG2 OFF\n" in candidate.writes


def test_reconnect_closes_candidate_when_boot_epoch_failure_escapes(
    oglo_usb, monkeypatch
):
    module, _ = oglo_usb
    stream_module = importlib.import_module(module.OgloTactileStream.__module__)
    stream = module.OgloTactileStream(
        "tactile_right", serial_port="/dev/ttyACM0", hand="right"
    )
    stream._set_tag_version(2)
    stream._stream_boot_id = "0" * 32
    stream._recording = True
    candidate = _FakeSerial("/dev/ttyACM0")
    candidate._to_read = b"#STREAM TAG2 on boot_id=" + b"1" * 32 + b"\r\n"
    monkeypatch.setattr(
        stream_module,
        "_open_usb_cdc",
        lambda *_args, **_kwargs: candidate,
    )

    with pytest.raises(Exception, match="rebooted during recording"):
        stream._usb_reconnect()

    assert candidate.closed


def test_tag_v2_ack_is_split_safe_and_keeps_first_binary_frame(oglo_usb):
    module, _ = oglo_usb
    boot_id = "0123456789abcdef0123456789abcdef"
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/ttyACM1", hand="left"
    )
    stream._stream_boot_id = boot_id
    frame = tag_v2(TAG_TYPE_TACTILE, 0, (1 << 32) + 1)

    class SplitSerial:
        chunks = [
            f"#STREAM TAG2 on boot_id={boot_id[:12]}".encode(),
            f"{boot_id[12:]}\r\n".encode() + frame,
        ]

        def read(self, _size):
            return self.chunks.pop(0) if self.chunks else b""

    assert stream._read_tag2_ack(SplitSerial()) == frame


def test_tag_v2_ack_skips_known_idle_text_race_and_keeps_binary(oglo_usb):
    module, _ = oglo_usb
    boot_id = "0123456789abcdef0123456789abcdef"
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/ttyACM1", hand="left"
    )
    stream._stream_boot_id = boot_id
    frame = tag_v2(TAG_TYPE_TACTILE, 0, (5 << 32) + 321)

    class RacingSerial:
        chunks = [
            b"#HB t_us=123 scan_us=2800\r\nDATA t_us=124\r\n",
            f"#STREAM TAG2 on boot_id={boot_id}\r\n".encode() + frame,
        ]

        def read(self, _size):
            return self.chunks.pop(0) if self.chunks else b""

    assert stream._read_tag2_ack(RacingSerial()) == frame


@pytest.mark.parametrize(
    "reply",
    [
        b"#STREAM TAG2 on\n",
        b"#STREAM TAG2 on boot_id=" + b"A" * 32 + b"\n",
        b"noise before ack\n",
        b"#ERR busy\n",
        b"\xa5\x5b\x01\x00\n",
    ],
)
def test_tag_v2_malformed_ack_fails_closed(oglo_usb, reply):
    module, _ = oglo_usb
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/ttyACM1", hand="left"
    )

    class BadSerial:
        def read(self, _size):
            return reply

    with pytest.raises(Exception, match="malformed TAG2"):
        stream._read_tag2_ack(BadSerial())


def test_connect_accepts_current_0_9_8_golden(oglo_usb, monkeypatch):
    module, _ = oglo_usb
    original = _FakeSerial.readline

    def current(self):
        cfg = {
            "device": "oglo", "schema_ver": 6, "side": "left",
            "fw_rev": "0.9.8", "hw_rev": "RDR02_FLEX5_REV_D_TIA",
        }
        return b"#CONFIG " + json.dumps(cfg).encode() + b"\n"

    monkeypatch.setattr(_FakeSerial, "readline", current)
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/ttyACM1", hand="left"
    )
    stream.connect()
    assert stream._manifest.fw_rev == "0.9.8"
    stream.disconnect()
    monkeypatch.setattr(_FakeSerial, "readline", original)


def test_recording_persists_independent_tactile_imu_mag(oglo_usb, tmp_path):
    module, holder = oglo_usb
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/ttyACM1", hand="left", output_dir=tmp_path
    )
    stream.connect()
    stream.start_recording(SessionClock(sync_point=SyncPoint.create_now("pi5"), recording_armed_ns=1_000_000))
    payload = b"".join(
        tag(kind, seq, 10_000 + seq * 1_000)
        for seq in range(5)
        for kind in (TAG_TYPE_TACTILE, TAG_TYPE_IMU, TAG_TYPE_MAG)
    )
    holder["serial"].feed(payload)
    deadline = time.monotonic() + 2
    while stream._frame_count < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    stream.stop_recording(); stream.disconnect()
    assert stream._frame_count == 5
    assert len((tmp_path / "tactile_left.imu.jsonl").read_text().splitlines()) == 5
    assert len((tmp_path / "tactile_left.mag.jsonl").read_text().splitlines()) == 5
    assert {a.stream_id for a in stream.recorded_artifacts()} == {"tactile_left.imu", "tactile_left.mag"}


def test_drop_detection_is_scoped_to_recording_window(oglo_usb, tmp_path):
    module, _ = oglo_usb
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/ttyACM1", hand="left", output_dir=tmp_path
    )
    health = []
    stream.on_health(health.append)
    stream.connect()

    # CDC startup/idle discontinuities are outside the sellable capture.
    stream._detect_drop("tactile", 10, 1, 1_000)
    stream._detect_drop("tactile", 100_000, 1, 2_000)
    assert health == []

    stream.start_recording(
        SessionClock(sync_point=SyncPoint.create_now("pi5"), recording_armed_ns=1_000_000)
    )
    # start_recording resets the idle baseline; only an in-window gap is loss.
    stream._detect_drop("tactile", 200_000, 1, 3_000)
    stream._detect_drop("tactile", 200_002, 1, 4_000)
    stream.stop_recording()
    stream.disconnect()

    assert len(health) == 1
    assert health[0].data == {"missing": 1, "seq_base": 200_002, "modality": "tactile"}


def test_impossible_sequence_jump_is_discarded_without_poisoning_baseline(oglo_usb):
    module, _ = oglo_usb
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/ttyACM1", hand="left"
    )
    health = []
    stream.on_health(health.append)
    stream._recording = True

    assert stream._detect_drop("imu", 100, 1, 1_000)
    assert not stream._detect_drop("imu", 0xFFF00000, 1, 2_000)
    assert stream._detect_drop("imu", 101, 1, 3_000)

    assert len(health) == 1
    assert health[0].kind.value == "warning"
    assert "discarded corrupt imu TAG frame" in health[0].detail
    assert stream._next_expected_seq["imu"] == 102


def test_sequence_wrap_is_continuous(oglo_usb):
    module, _ = oglo_usb
    stream = module.OgloTactileStream(
        "tactile_left", serial_port="/dev/ttyACM1", hand="left"
    )
    health = []
    stream.on_health(health.append)
    stream._recording = True

    assert stream._detect_drop("imu", 0xFFFFFFFF, 1, 1_000)
    assert stream._detect_drop("imu", 0, 1, 2_000)
    assert health == []


def test_rejects_wrong_firmware(oglo_usb, monkeypatch):
    module, _ = oglo_usb
    original = _FakeSerial.readline
    def old(self):
        cfg = {"device":"oglo", "schema_ver":6, "side":"left", "fw_rev":"0.9.2"}
        return b"#CONFIG " + json.dumps(cfg).encode() + b"\n"
    monkeypatch.setattr(_FakeSerial, "readline", old)
    with pytest.raises(Exception, match="0.9.3"):
        module.OgloTactileStream("x", serial_port="/dev/ttyACM1", hand="left").connect()
    monkeypatch.setattr(_FakeSerial, "readline", original)


@pytest.mark.parametrize("fw_rev", ["", "0.9.3-rc1", "v0.9.8", "0.9"])
def test_rejects_malformed_or_prerelease_firmware(oglo_usb, monkeypatch, fw_rev):
    module, _ = oglo_usb

    def invalid(self):
        cfg = {"device": "oglo", "schema_ver": 6, "side": "left", "fw_rev": fw_rev}
        return b"#CONFIG " + json.dumps(cfg).encode() + b"\n"

    monkeypatch.setattr(_FakeSerial, "readline", invalid)
    with pytest.raises(Exception, match=">=0.9.3"):
        module.OgloTactileStream(
            "x", serial_port="/dev/ttyACM1", hand="left"
        ).connect()
