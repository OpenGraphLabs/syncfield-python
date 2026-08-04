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

from syncfield.adapters.oglo.usb_packet import TAG_MAGIC, TAG_TYPE_IMU, TAG_TYPE_MAG, TAG_TYPE_TACTILE
from syncfield.clock import SessionClock
from syncfield.types import SyncPoint


def tag(kind: int, seq: int, t_us: int, values=None) -> bytes:
    counts = {TAG_TYPE_TACTILE: 80, TAG_TYPE_IMU: 6, TAG_TYPE_MAG: 3}
    formats = {TAG_TYPE_TACTILE: "<80H", TAG_TYPE_IMU: "<6h", TAG_TYPE_MAG: "<3h"}
    values = values if values is not None else list(range(counts[kind]))
    payload = struct.pack(formats[kind], *values)
    return TAG_MAGIC + bytes([kind]) + struct.pack("<HII", len(payload), seq, t_us) + payload


class _FakeSerial:
    def __init__(self, port, baud, timeout=0.1):
        self.port, self.writes, self.closed = port, [], False
        self._lock = threading.Lock()
        self._to_read = tag(TAG_TYPE_TACTILE, 0, 1000)

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
    def ctor(port, baud, timeout=0.1):
        holder["serial"] = _FakeSerial(port, baud, timeout); return holder["serial"]
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
    assert stream._manifest.schema_ver == 6
    assert any(b"GET CONFIG" in write for write in fake.writes)
    assert any(b"STREAM TAG ON" in write for write in fake.writes)
    assert samples
    stream.disconnect()
    assert any(b"STREAM TAG OFF" in write for write in fake.writes)
    assert fake.closed


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
