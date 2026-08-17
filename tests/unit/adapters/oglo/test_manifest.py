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
    assert m.tag_ver_max == 1
    assert m.boot_id == ""
    assert m.link_ping is False
    assert m.supports_link_ping is False


@pytest.mark.parametrize(
    ("fw_rev", "capability", "expected"),
    [
        ("0.9.15", True, False),
        ("0.9.16", False, False),
        ("0.9.16", True, True),
        ("0.9.17", True, True),
        ("1.0.0", True, True),
        ("0.9.16", "true", False),
        ("0.9.16", 1, False),
    ],
)
def test_link_ping_requires_exact_capability_and_minimum_firmware(
    fw_rev, capability, expected
):
    manifest = OgloDeviceManifest.from_json(
        _manifest(fw_rev=fw_rev, link_ping=capability)
    )
    assert manifest.link_ping is (capability is True)
    assert manifest.supports_link_ping is expected


def test_tag_v2_capability_and_boot_identity_are_transport_metadata():
    boot_id = "0123456789abcdef0123456789abcdef"
    m = OgloDeviceManifest.from_json(
        _manifest(fw_rev="0.9.13", tag_ver_max=2, boot_id=boot_id)
    )
    assert m.schema_ver == 6
    assert m.tag_ver_max == 2
    assert m.boot_id == boot_id


@pytest.mark.parametrize(
    "tag_ver_max", [0, -1, 256, True, "2", "nope", 2.9, None]
)
def test_invalid_tag_version_capability_is_rejected(tag_ver_max):
    with pytest.raises(OgloProtocolError, match="tag_ver_max"):
        OgloDeviceManifest.from_json(_manifest(tag_ver_max=tag_ver_max))


@pytest.mark.parametrize(
    "boot_id",
    ["short", "z" * 32, "00-11", True, -1, 1 << 128, 1.5, []],
)
def test_invalid_boot_identity_is_rejected(boot_id):
    with pytest.raises(OgloProtocolError, match="boot_id"):
        OgloDeviceManifest.from_json(_manifest(boot_id=boot_id))


def test_integer_boot_identity_is_canonicalized_as_128_bit_hex():
    manifest = OgloDeviceManifest.from_json(_manifest(boot_id=0xABCD))
    assert manifest.boot_id == "0000000000000000000000000000abcd"
