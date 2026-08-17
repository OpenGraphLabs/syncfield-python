"""OGLO schema-6 USB TAG parser — pure, no serial/hardware."""

import struct

import pytest

from syncfield.adapters.oglo.packet import OgloProtocolError
from syncfield.adapters.oglo.usb_packet import (
    TAG_HEADER_LEN,
    TAG_MAGIC,
    TAG_V2_HEADER_LEN,
    TAG_V2_MAGIC,
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


def packed_tactile_packet(values, seq: int = 7, t_us: int = 1234) -> bytes:
    payload = bytearray()
    for a, b in zip(values[0::2], values[1::2]):
        payload.extend((a >> 4, ((a & 0x0F) << 4) | (b >> 8), b & 0xFF))
    return TAG_MAGIC + bytes([TAG_TYPE_TACTILE]) + struct.pack("<HII", len(payload), seq, t_us) + payload


def packet_v2(kind: int, seq: int = 7, t_us: int = 1234, values=()) -> bytes:
    formats = {TAG_TYPE_TACTILE: "<80H", TAG_TYPE_IMU: "<6h", TAG_TYPE_MAG: "<3h"}
    if not values:
        values = [0] * {TAG_TYPE_TACTILE: 80, TAG_TYPE_IMU: 6, TAG_TYPE_MAG: 3}[kind]
    payload = struct.pack(formats[kind], *values)
    return TAG_V2_MAGIC + bytes([kind]) + struct.pack("<HIQ", len(payload), seq, t_us) + payload


@pytest.mark.parametrize("kind,count", [(1, 80), (2, 6), (3, 3)])
def test_decodes_each_independent_modality(kind, count):
    p = parse_usb_packet(packet(kind, seq=42, t_us=2_000_000, values=range(count)))
    assert p.stream_type == kind
    assert p.seq == 42
    assert p.device_ns == 2_000_000_000
    assert p.values == tuple(range(count))


def test_decodes_0_9_9_packed12_tactile():
    values = tuple((i * 51) & 0xFFF for i in range(80))
    p = parse_usb_packet(packed_tactile_packet(values, seq=99))
    assert p.seq == 99
    assert p.values == values


def test_decodes_tag_v2_native_u64_timestamp():
    timestamp = (1 << 32) + 987_654
    p = parse_usb_packet(packet_v2(TAG_TYPE_IMU, seq=42, t_us=timestamp, values=range(6)))
    assert p.tag_version == 2
    assert p.seq == 42
    assert p.device_us == timestamp
    assert p.device_ns == timestamp * 1_000
    assert TAG_V2_HEADER_LEN == 17


def test_tag_v2_rejects_the_legacy_wide_tactile_payload():
    wide = packet_v2(TAG_TYPE_TACTILE)
    with pytest.raises(OgloProtocolError, match="payload"):
        parse_usb_packet(wide)

    parsed, leftover = iter_usb_packets(
        wide + packet_v2(TAG_TYPE_IMU, seq=9, t_us=(2 << 32) + 1)
    )
    packets = list(parsed)
    assert [(p.tag_version, p.modality, p.seq) for p in packets] == [
        (2, "imu", 9)
    ]
    assert leftover == b""


def test_iterator_decodes_mixed_v1_v2_during_upgrade_resync():
    parsed, leftover = iter_usb_packets(
        b"ascii" + packet(TAG_TYPE_TACTILE, seq=1, t_us=0xFFFFFFF0)
        + b"noise" + packet_v2(TAG_TYPE_IMU, seq=2, t_us=(1 << 32) + 10)
    )
    packets = list(parsed)
    assert [(p.tag_version, p.modality, p.seq) for p in packets] == [
        (1, "tactile", 1),
        (2, "imu", 2),
    ]
    assert leftover == b""


@pytest.mark.parametrize("partial", [b"\xA5", TAG_V2_MAGIC + b"\x02\x0c"])
def test_iterator_preserves_partial_v2_header(partial):
    parsed, leftover = iter_usb_packets(b"junk" + partial)
    assert list(parsed) == []
    assert leftover == partial


@pytest.mark.parametrize("version", [1, 2])
def test_every_partial_frame_prefix_is_buffered_and_never_decoded(version):
    """Firmware fail-close may end a USB session at any byte of a frame."""
    frame = (
        packet(TAG_TYPE_IMU, seq=41, t_us=9000, values=range(6))
        if version == 1
        else packet_v2(
            TAG_TYPE_IMU, seq=41, t_us=(2 << 32) + 9000, values=range(6)
        )
    )
    for cut in range(1, len(frame)):
        parsed, leftover = iter_usb_packets(frame[:cut])
        assert list(parsed) == [], cut
        assert leftover == frame[:cut], cut


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
