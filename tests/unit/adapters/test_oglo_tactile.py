"""OGLO v6 stream routing that does not need a serial thread."""

import importlib
import json
import sys
from unittest.mock import MagicMock

import pytest

from syncfield.adapters.oglo.usb_packet import TAG_TYPE_IMU, TAG_TYPE_MAG, TAG_TYPE_TACTILE, UsbTaggedPacket


@pytest.fixture
def stream_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "bleak", MagicMock())
    sys.modules.pop("syncfield.adapters.oglo.stream", None)
    module = importlib.import_module("syncfield.adapters.oglo.stream")
    yield module
    sys.modules.pop("syncfield.adapters.oglo.stream", None)


def make_stream(module, tmp_path, side="left"):
    stream = module.OgloTactileStream(
        f"tactile_{side}", serial_port="/dev/ttyTEST", hand=side, output_dir=tmp_path
    )
    stream._apply_manifest(module.OgloDeviceManifest.from_json(json.dumps({
        "device": "oglo", "schema_ver": 6, "side": side, "fw_rev": "0.9.3",
        "rate_hz": 250, "values_per_sample": 80, "sample_shape": [5, 4, 4],
    })))
    return stream


def test_schema6_manifest_controls_side_aware_taxel_labels(stream_module, tmp_path):
    stream = make_stream(stream_module, tmp_path, "left")
    assert stream.hand == "left"
    assert stream._channel_labels[0].startswith("pinky_")
    assert len(stream._channel_labels) == 80


def test_each_modality_has_independent_drop_detection(stream_module, tmp_path):
    stream = make_stream(stream_module, tmp_path)
    stream._recording = True
    stream._handle_usb_packet(UsbTaggedPacket(TAG_TYPE_TACTILE, 10, 1, tuple(range(80))))
    stream._handle_usb_packet(UsbTaggedPacket(TAG_TYPE_IMU, 20, 1, tuple(range(6))))
    stream._handle_usb_packet(UsbTaggedPacket(TAG_TYPE_TACTILE, 12, 2, tuple(range(80))))
    stream._handle_usb_packet(UsbTaggedPacket(TAG_TYPE_IMU, 22, 2, tuple(range(6))))
    drops = [e for e in stream._collected_health if e.kind.value == "drop"]
    assert {e.data["modality"] for e in drops} == {"tactile", "imu"}
    assert all(e.data["missing"] == 1 for e in drops)


def test_idle_sequence_gaps_do_not_pollute_recording_health(stream_module, tmp_path):
    stream = make_stream(stream_module, tmp_path)
    stream._handle_usb_packet(UsbTaggedPacket(TAG_TYPE_TACTILE, 10, 1, tuple(range(80))))
    stream._handle_usb_packet(UsbTaggedPacket(TAG_TYPE_TACTILE, 12, 2, tuple(range(80))))

    assert not [e for e in stream._collected_health if e.kind.value == "drop"]


def test_substream_contract_includes_500hz_imu_and_125hz_mag(stream_module, tmp_path):
    stream = make_stream(stream_module, tmp_path)
    assert [(s.id, s.expected_hz) for s in stream.substreams()] == [
        ("tactile_left.imu", 500.0), ("tactile_left.mag", 125.0)
    ]


def test_mag_callback_uses_its_own_timestamp_and_sequence(stream_module, tmp_path):
    stream = make_stream(stream_module, tmp_path)
    events = []
    stream.on_substream_sample(events.append)
    stream._handle_usb_packet(UsbTaggedPacket(TAG_TYPE_MAG, 9, 123_456, (-1, 2, -3)))
    assert events[0].stream_id == "tactile_left.mag"
    assert events[0].device_ns == 123_456_000
    assert events[0].channels == {"mx": -1, "my": 2, "mz": -3}
