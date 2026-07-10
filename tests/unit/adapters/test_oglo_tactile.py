"""Unit tests for the OGLO tactile adapter (firmware v5 / packed12).

The decode and recording paths are exercised synchronously via
``_dispatch_notification_for_test`` (no BLE). The BLE lifecycle (connect →
manifest validation → subscribe) is exercised through a small async fake
``bleak`` client.
"""

from __future__ import annotations

import importlib
import json
import struct
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from syncfield.clock import SessionClock
from syncfield.types import SyncPoint

_MANIFEST = json.dumps(
    {
        "device": "oglo",
        "schema_ver": 5,
        "serial": "OGLO-0001",
        "side": "right",
        "fw_rev": "0.7.1-cfgfit",
        "rate_hz": 100,
        "values_per_sample": 80,
        "sample_shape": [5, 4, 4],
        "channels": ["thumb", "index", "middle", "ring", "pinky"],
    }
).encode("utf-8")

_BAD_MANIFEST = json.dumps({"schema_ver": 4, "side": "right"}).encode("utf-8")


# ---------------------------------------------------------------------------
# Packet builder (mirror of the firmware-side packer)
# ---------------------------------------------------------------------------


def _pack_taxels12(taxels: list[int]) -> bytes:
    out = bytearray()
    for k in range((len(taxels) + 1) // 2):
        a = taxels[2 * k] & 0x0FFF
        b = (taxels[2 * k + 1] & 0x0FFF) if (2 * k + 1) < len(taxels) else 0
        out.append(a >> 4)
        out.append(((a & 0x0F) << 4) | (b >> 8))
        out.append(b & 0xFF)
    return bytes(out)


def _ramp(n: int = 80) -> list[int]:
    return [(i * 51) & 0x0FFF for i in range(n)]


def _build_packet(samples, *, seq_base=1000, t_base_us=5_000_000, flags=0x04) -> bytes:
    payload = bytearray(struct.pack("<BBII", len(samples), flags, seq_base, t_base_us))
    for dt_us, taxels, imu in samples:
        payload += struct.pack("<H", dt_us)
        payload += _pack_taxels12(taxels)
        payload += struct.pack("<6h", *imu)
    return bytes(payload)


def _default_packet() -> bytes:
    return _build_packet(
        [
            (0, _ramp(), (1, 2, 3, -4, -5, -6)),
            (10_000, _ramp(), (7, 8, 9, -10, -11, -12)),
            (20_000, _ramp(), (13, 14, 15, -16, -17, -18)),
        ]
    )


# ---------------------------------------------------------------------------
# Fake bleak
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal async BleakClient stand-in for lifecycle tests."""

    manifest_bytes = _MANIFEST

    def __init__(self, device):
        self.device = device
        self.notify_cb = None
        self.connected = False

    async def connect(self):
        self.connected = True

    async def read_gatt_char(self, uuid):
        return bytearray(type(self).manifest_bytes)

    async def start_notify(self, uuid, cb):
        self.notify_cb = cb

    async def stop_notify(self, uuid):
        self.notify_cb = None

    async def disconnect(self):
        self.connected = False


@pytest.fixture
def oglo_module(monkeypatch):
    """Install a fake ``bleak`` so the adapter package imports without hardware."""
    fake = MagicMock()
    fake.BleakClient = _FakeClient
    monkeypatch.setitem(sys.modules, "bleak", fake)
    for m in (
        "syncfield.adapters.oglo",
        "syncfield.adapters.oglo.stream",
    ):
        sys.modules.pop(m, None)
    module = importlib.import_module("syncfield.adapters.oglo")
    yield module
    for m in (
        "syncfield.adapters.oglo",
        "syncfield.adapters.oglo.stream",
    ):
        sys.modules.pop(m, None)


def _apply_default_manifest(stream) -> None:
    from syncfield.adapters.oglo.manifest import OgloDeviceManifest

    stream._apply_manifest(OgloDeviceManifest.from_json(_MANIFEST))


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_capabilities(self, oglo_module):
        s = oglo_module.OgloTactileStream("oglo", address="addr")
        caps = s.capabilities
        assert caps.provides_audio_track is False
        assert caps.supports_precise_timestamps is True
        assert caps.is_removable is True
        assert caps.produces_file is False
        assert s.kind == "sensor"

    def test_hand_hint_default(self, oglo_module):
        s = oglo_module.OgloTactileStream("oglo", address="addr", hand="left")
        assert s.hand == "left"

    def test_uuid_constants(self, oglo_module):
        assert oglo_module.SERVICE_UUID == "4652535f-424c-4500-0000-000000000001"
        assert oglo_module.NOTIFY_CHAR_UUID == "4652535f-424c-4500-0001-000000000001"
        assert oglo_module.CONFIG_CHAR_UUID == "4652535f-424c-4500-0002-000000000001"

    def test_requires_address_or_name(self, oglo_module):
        with pytest.raises(ValueError):
            oglo_module.OgloTactileStream("oglo", address=None, ble_name="")

    @pytest.mark.parametrize("empty", ["", "   ", "\t"])
    def test_an_empty_address_means_not_supplied(self, oglo_module, empty):
        """`prepare()` used to test `self._address is not None`, so `""` — what
        an unset config field looks like; og-skill's kiosk read exactly that out
        of /etc/og-kiosk/device.json — skipped the name scan and handed the
        empty string to bleak as the target device."""
        s = oglo_module.OgloTactileStream("oglo", address=empty, hand="left")
        assert s._address is None

    def test_an_address_is_stripped_not_dialled_with_whitespace(self, oglo_module):
        s = oglo_module.OgloTactileStream("oglo", address="  AA:BB  ")
        assert s._address == "AA:BB"

    def test_prepare_with_an_empty_address_scans_by_name_and_hand(self, oglo_module, monkeypatch):
        left = SimpleNamespace(name="OGLO_LEFT_TEST01", address="28:84:85:BB:60:ED")
        right = SimpleNamespace(name="OGLO RIGHT", address="28:84:85:BB:AF:3D")
        adv = SimpleNamespace(local_name=None)

        async def fake_discover(timeout, return_adv):  # noqa: ARG001
            # Right advertises first: the old first-match code would hand it to
            # the left stream.
            return {right.address: (right, adv), left.address: (left, adv)}

        # `oglo_module` is the package; the fake `bleak` lives on the stream
        # module that actually imported it.
        stream_module = sys.modules["syncfield.adapters.oglo.stream"]
        monkeypatch.setattr(
            stream_module.bleak, "BleakScanner", SimpleNamespace(discover=fake_discover)
        )

        left_stream = oglo_module.OgloTactileStream("tactile_left", address="", hand="left")
        right_stream = oglo_module.OgloTactileStream("tactile_right", address="", hand="right")
        left_stream.prepare()
        right_stream.prepare()

        assert left_stream._device is left
        assert right_stream._device is right

    def test_prepare_raises_when_the_hand_is_not_on_the_air(self, oglo_module, monkeypatch):
        left = SimpleNamespace(name="OGLO LEFT", address="AA")
        adv = SimpleNamespace(local_name=None)

        async def fake_discover(timeout, return_adv):  # noqa: ARG001
            return {left.address: (left, adv)}

        # `oglo_module` is the package; the fake `bleak` lives on the stream
        # module that actually imported it.
        stream_module = sys.modules["syncfield.adapters.oglo.stream"]
        monkeypatch.setattr(
            stream_module.bleak, "BleakScanner", SimpleNamespace(discover=fake_discover)
        )

        stream = oglo_module.OgloTactileStream("tactile_right", address="", hand="right")
        with pytest.raises(RuntimeError, match="not found"):
            stream.prepare()

    def test_discovery_metadata(self, oglo_module):
        cls = oglo_module.OgloTactileStream
        assert cls._discovery_kind == "sensor"
        assert cls._discovery_adapter_type == "oglo_tactile"


# ---------------------------------------------------------------------------
# Decode (preview phase — no BLE, no recording)
# ---------------------------------------------------------------------------


class TestDecode:
    def test_taxel_channels_and_device_ns(self, oglo_module):
        s = oglo_module.OgloTactileStream("oglo", address="addr")
        _apply_default_manifest(s)
        got = []
        s.on_sample(got.append)
        s._dispatch_notification_for_test(_default_packet())

        assert len(got) == 3
        first = got[0]
        assert first.frame_number == -1  # preview sentinel
        assert first.device_ns == 5_000_000_000
        assert got[1].device_ns == 5_010_000_000
        # 80 taxel channels, side-aware labels.
        assert len(first.channels) == 80
        assert "thumb_0_0" in first.channels
        assert "pinky_3_3" in first.channels
        assert first.channels["thumb_0_1"] == (1 * 51)

    def test_imu_routed_to_substream_not_primary(self, oglo_module):
        s = oglo_module.OgloTactileStream("oglo", address="addr")
        _apply_default_manifest(s)
        taxels, imus = [], []
        s.on_sample(taxels.append)
        s.on_substream_sample(imus.append)
        s._dispatch_notification_for_test(_default_packet())

        assert len(imus) == 3
        assert imus[0].stream_id == "oglo.imu"
        assert imus[0].channels == {
            "ax": 1, "ay": 2, "az": 3, "gx": -4, "gy": -5, "gz": -6
        }
        assert imus[0].device_ns == 5_000_000_000
        # Primary taxel samples must not carry IMU channels.
        assert "ax" not in taxels[0].channels

    def test_bad_flags_emit_warning_not_crash(self, oglo_module):
        s = oglo_module.OgloTactileStream("oglo", address="addr")
        _apply_default_manifest(s)
        got = []
        s.on_sample(got.append)
        s._dispatch_notification_for_test(
            _build_packet([(0, _ramp(), (0, 0, 0, 0, 0, 0))], flags=0x02)
        )
        assert got == []
        assert any(h.kind.value == "warning" for h in s._collected_health)

    def test_substreams_surface(self, oglo_module):
        s = oglo_module.OgloTactileStream("oglo", address="addr")
        subs = s.substreams()
        assert len(subs) == 1
        assert subs[0].id == "oglo.imu"
        assert subs[0].kind == "sensor"


# ---------------------------------------------------------------------------
# Recording (taxel counters + IMU substream file)
# ---------------------------------------------------------------------------


class TestRecording:
    def _record_one_packet(self, s, tmp_path):
        _apply_default_manifest(s)
        s._output_dir = tmp_path
        s._rebind_output_paths()
        s._open_imu_writer()
        s._imu_frame_count = 0
        clock = SessionClock(
            sync_point=SyncPoint.create_now("h"),
            recording_armed_ns=1_000_000,
        )
        s._begin_recording_window(clock)
        s._recording = True
        s._dispatch_notification_for_test(_default_packet())
        return s.stop_recording()

    def test_counters_and_report(self, oglo_module, tmp_path):
        s = oglo_module.OgloTactileStream("oglo", address="addr", output_dir=tmp_path)
        report = self._record_one_packet(s, tmp_path)
        assert report.status == "completed"
        assert report.frame_count == 3
        assert report.first_sample_at_ns is not None
        assert report.recording_anchor is not None
        assert report.recording_anchor.first_frame_device_ns == 5_000_000_000

    def test_imu_jsonl_written(self, oglo_module, tmp_path):
        s = oglo_module.OgloTactileStream("oglo", address="addr", output_dir=tmp_path)
        self._record_one_packet(s, tmp_path)

        imu_file = tmp_path / "oglo.imu.jsonl"
        assert imu_file.exists()
        rows = [json.loads(line) for line in imu_file.read_text().splitlines()]
        assert len(rows) == 3
        assert rows[0]["channels"] == {
            "ax": 1, "ay": 2, "az": 3, "gx": -4, "gy": -5, "gz": -6
        }
        assert rows[0]["device_timestamp_ns"] == 5_000_000_000
        assert rows[0]["frame_number"] == 0

        artifacts = s.recorded_artifacts()
        assert len(artifacts) == 1
        assert artifacts[0].stream_id == "oglo.imu"
        assert artifacts[0].frame_count == 3

    def test_calibration_file_written_for_authoritative_side(self, oglo_module, tmp_path):
        from syncfield.oglo_calibration import OGLO_CALIBRATION_SCHEMA

        full = {
            "schema": OGLO_CALIBRATION_SCHEMA,
            "rig_id": "OGLO-MT-CASE-07",
            "units": "meters",
            "convention": "T_wrist_from_marker",
            "usage": "wrist pose",
            "hands": {
                "right": {
                    "wrist_frame": "right_wrist",
                    "markers": [{"id": 4}, {"id": 5}, {"id": 6}, {"id": 7}],
                    "imu": {"part": "ICM-42688-P", "orientation_confirmed": True},
                },
                "left": {
                    "wrist_frame": "left_wrist",
                    "markers": [{"id": 0}, {"id": 1}, {"id": 2}, {"id": 3}],
                    "imu": {"part": "ICM-42688-P", "orientation_confirmed": False, "R": None},
                },
            },
            "source": {"file": "oglo_marker_extrinsics.json"},
        }
        s = oglo_module.OgloTactileStream(
            "oglo", address="addr", output_dir=tmp_path, calibration=full, hand="left"
        )
        # Authoritative side (manifest) wins over the construct-time hint.
        s._hand = "right"
        s._output_dir = tmp_path
        s._write_calibration_file()

        cal = tmp_path / "oglo.calibration.json"
        assert cal.exists()
        doc = json.loads(cal.read_text())
        assert doc["schema"] == OGLO_CALIBRATION_SCHEMA
        assert doc["side"] == "right"
        assert doc["wrist_frame"] == "right_wrist"
        assert [m["id"] for m in doc["markers"]] == [4, 5, 6, 7]
        assert doc["imu"]["orientation_confirmed"] is True
        assert "hands" not in doc

    def test_no_calibration_file_without_calibration_or_side(self, oglo_module, tmp_path):
        s = oglo_module.OgloTactileStream("oglo", address="addr", output_dir=tmp_path)
        s._output_dir = tmp_path
        s._hand = "right"
        s._write_calibration_file()  # no calibration -> no file
        assert not (tmp_path / "oglo.calibration.json").exists()

        s2 = oglo_module.OgloTactileStream(
            "oglo", address="addr", output_dir=tmp_path, calibration={"hands": {"right": {}}}
        )
        s2._output_dir = tmp_path
        s2._hand = "unknown"  # no authoritative side -> no file
        s2._write_calibration_file()
        assert not (tmp_path / "oglo.calibration.json").exists()

    def test_consecutive_recordings_follow_rotated_output_dir(self, oglo_module, tmp_path):
        s = oglo_module.OgloTactileStream("oglo", address="addr", output_dir=tmp_path)
        ep1 = tmp_path / "ep1"
        ep1.mkdir()
        s._output_dir = ep1
        self._record_one_packet_into(s, ep1)
        assert (ep1 / "oglo.imu.jsonl").exists()

        # Orchestrator rotates _output_dir for the next episode.
        ep2 = tmp_path / "ep2"
        ep2.mkdir()
        s._output_dir = ep2
        self._record_one_packet_into(s, ep2)
        assert (ep2 / "oglo.imu.jsonl").exists()

    def _record_one_packet_into(self, s, out_dir):
        _apply_default_manifest(s)
        s._rebind_output_paths()
        s._open_imu_writer()
        s._imu_frame_count = 0
        clock = SessionClock(
            sync_point=SyncPoint.create_now("h"),
            recording_armed_ns=1_000_000,
        )
        s._begin_recording_window(clock)
        s._recording = True
        s._dispatch_notification_for_test(_default_packet())
        s.stop_recording()


# ---------------------------------------------------------------------------
# BLE lifecycle via the async fake client
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_connect_reads_manifest_and_applies_side(self, oglo_module):
        s = oglo_module.OgloTactileStream("oglo", address="addr", hand="unknown")
        s.connect()
        try:
            assert s.manifest is not None
            assert s.manifest.schema_ver == 5
            # Manifest side overrides the hand hint.
            assert s.hand == "right"
        finally:
            s.disconnect()

    def test_connect_fails_loud_on_bad_schema(self, oglo_module, monkeypatch):
        monkeypatch.setattr(_FakeClient, "manifest_bytes", _BAD_MANIFEST)
        s = oglo_module.OgloTactileStream("oglo", address="addr")
        with pytest.raises(oglo_module.OgloProtocolError, match="schema_ver"):
            s.connect()

    def test_notify_decodes_after_connect(self, oglo_module):
        s = oglo_module.OgloTactileStream("oglo", address="addr")
        got = []
        s.on_sample(got.append)
        s.connect()
        try:
            # Drive a packet the way the BLE notify handler would.
            s._handle_payload(_default_packet())
            assert len(got) == 3
        finally:
            s.disconnect()


# ---------------------------------------------------------------------------
# Lazy export / import guard
# ---------------------------------------------------------------------------


class TestLazyExport:
    def test_exported_from_adapters(self, oglo_module):
        import syncfield.adapters as adapters

        importlib.reload(adapters)
        assert "OgloTactileStream" in adapters.__all__


class TestImportGuard:
    def test_missing_bleak_raises_install_hint(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "bleak", None)
        sys.modules.pop("syncfield.adapters.oglo", None)
        sys.modules.pop("syncfield.adapters.oglo.stream", None)
        with pytest.raises(ImportError, match=r"syncfield\[ble\]"):
            importlib.import_module("syncfield.adapters.oglo.stream")
        sys.modules.pop("syncfield.adapters.oglo", None)
        sys.modules.pop("syncfield.adapters.oglo.stream", None)
