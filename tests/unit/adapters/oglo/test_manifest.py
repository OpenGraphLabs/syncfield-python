"""Unit tests for the OGLO schema-6 config manifest parser."""

from __future__ import annotations

import json

import pytest

from syncfield.adapters.oglo.manifest import OgloDeviceManifest
from syncfield.adapters.oglo.packet import OgloProtocolError


def _manifest(**overrides) -> str:
    base = {
        "device": "oglo",
        "schema_ver": 6,
        "serial": "OGLO-0001",
        "side": "right",
        "fw_rev": "0.9.3",
        "rate_hz": 250,
        "values_per_sample": 80,
        "sample_order": "finger,row,col",
        "sample_shape": [5, 4, 4],
        "channels": ["thumb", "index", "middle", "ring", "pinky"],
        "adc_bits": 12,
    }
    base.update(overrides)
    return json.dumps(base)


def test_parse_full_schema6_manifest():
    m = OgloDeviceManifest.from_json(_manifest())
    assert m.device == "oglo"
    assert m.side == "right"
    assert m.schema_ver == 6
    assert m.rate_hz == 250
    assert m.values_per_sample == 80
    assert m.sample_shape == (5, 4, 4)
    assert m.finger_labels == ("thumb", "index", "middle", "ring", "pinky")
    assert m.fw_rev == "0.9.3"


def test_accepts_bytes():
    m = OgloDeviceManifest.from_json(_manifest().encode("utf-8"))
    assert m.side == "right"


def test_channel_label_right_hand():
    m = OgloDeviceManifest.from_json(_manifest(side="right"))
    assert m.channel_label(0) == "thumb_0_0"
    assert m.channel_label(1) == "thumb_0_1"
    assert m.channel_label(4) == "thumb_1_0"  # rows=4, cols=4 → row wraps at 4
    assert m.channel_label(15) == "thumb_3_3"
    assert m.channel_label(16) == "index_0_0"
    assert m.channel_label(79) == "pinky_3_3"


def test_channel_labels_length_and_order():
    m = OgloDeviceManifest.from_json(_manifest())
    labels = m.channel_labels()
    assert len(labels) == 80
    assert labels[0] == "thumb_0_0"
    assert labels[-1] == "pinky_3_3"


def test_left_hand_default_finger_order_when_channels_missing():
    raw = _manifest(side="left")
    data = json.loads(raw)
    del data["channels"]
    m = OgloDeviceManifest.from_json(json.dumps(data))
    # Left hand mirrors: pinky first.
    assert m.finger_labels == ("pinky", "ring", "middle", "index", "thumb")
    assert m.channel_label(0) == "pinky_0_0"


def test_unsupported_schema_raises():
    with pytest.raises(OgloProtocolError, match="schema_ver"):
        OgloDeviceManifest.from_json(_manifest(schema_ver=4))


def test_missing_schema_raises():
    data = json.loads(_manifest())
    del data["schema_ver"]
    with pytest.raises(OgloProtocolError, match="schema_ver"):
        OgloDeviceManifest.from_json(json.dumps(data))


def test_invalid_json_raises():
    with pytest.raises(OgloProtocolError, match="not valid JSON"):
        OgloDeviceManifest.from_json(b"\x00\x01not json")


def test_unknown_keys_ignored():
    m = OgloDeviceManifest.from_json(_manifest(pair_id="p1", cal_valid=True, future="x"))
    assert m.side == "right"
    assert m.raw["future"] == "x"


def test_defaults_when_fields_absent():
    m = OgloDeviceManifest.from_json(json.dumps({"schema_ver": 6}))
    assert m.side == "unknown"
    assert m.rate_hz == 250
    assert m.values_per_sample == 80
    assert m.sample_shape == (5, 4, 4)
    # Unknown side falls back to the right-hand canonical order.
    assert m.finger_labels[0] == "thumb"
