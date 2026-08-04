"""OGLO schema-6 USB TAG parser — pure, no serial/hardware."""

import struct

import pytest

from syncfield.adapters.oglo.packet import OgloProtocolError
from syncfield.adapters.oglo.usb_packet import (
    TAG_HEADER_LEN,
    TAG_MAGIC,
    TAG_TYPE_IMU,
    TAG_TYPE_MAG,
    TAG_TYPE_TACTILE,
    iter_usb_packets,
    parse_usb_packet,
)


def packet(kind: int, seq: int = 7, t_us: int = 1234, values=()) -> bytes:
    formats = {TAG_TYPE_TACTILE: "<80H", TAG_TYPE_IMU: "<6h", TAG_TYPE_MAG: "<3h"}
    if not values:
        values = [0] * {TAG_TYPE_TACTILE: 80, TAG_TYPE_IMU: 6, TAG_TYPE_MAG: 3}[kind]
    payload = struct.pack(formats[kind], *values)
    return TAG_MAGIC + bytes([kind]) + struct.pack("<HII", len(payload), seq, t_us) + payload


@pytest.mark.parametrize("kind,count", [(1, 80), (2, 6), (3, 3)])
def test_decodes_each_independent_modality(kind, count):
    p = parse_usb_packet(packet(kind, seq=42, t_us=2_000_000, values=range(count)))
    assert p.stream_type == kind
    assert p.seq == 42
    assert p.device_ns == 2_000_000_000
    assert p.values == tuple(range(count))


def test_rejects_unknown_type_and_wrong_length():
    bad_type = TAG_MAGIC + b"\x09" + struct.pack("<HII", 0, 0, 0)
    with pytest.raises(OgloProtocolError, match="unknown"):
        parse_usb_packet(bad_type)
    bad_len = bytearray(packet(TAG_TYPE_IMU))
    bad_len[3:5] = struct.pack("<H", 10)
    with pytest.raises(OgloProtocolError, match="payload"):
        parse_usb_packet(bytes(bad_len))


def test_iterator_resyncs_and_preserves_partial_tail():
    first = packet(TAG_TYPE_TACTILE, seq=1)
    second = packet(TAG_TYPE_IMU, seq=2)
    partial = packet(TAG_TYPE_MAG, seq=3)[:8]
    parsed, leftover = iter_usb_packets(b"#STREAM tag on\r\n" + first + b"garbage" + second + partial)
    assert [(p.modality, p.seq) for p in parsed] == [("tactile", 1), ("imu", 2)]
    assert leftover == partial


def test_magic_inside_payload_does_not_split_frame():
    values = [0] * 80
    values[4] = 0x5AA5
    parsed, leftover = iter_usb_packets(packet(TAG_TYPE_TACTILE, 1, values=values) + packet(TAG_TYPE_MAG, 2))
    assert [p.seq for p in parsed] == [1, 2]
    assert leftover == b""


def test_keeps_partial_magic():
    parsed, leftover = iter_usb_packets(b"noise\xA5")
    assert list(parsed) == []
    assert leftover == b"\xA5"
