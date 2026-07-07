"""Unit tests for the pure packed12 v5 packet parser (no BLE required)."""

from __future__ import annotations

import struct

import pytest

from syncfield.adapters.oglo.packet import (
    FLAG_PACKED,
    HEADER_LEN,
    IMU_RAW_LEN,
    SAMPLE_STRIDE,
    OgloProtocolError,
    parse_v5,
    unpack_taxels12,
)


def _pack_taxels12(taxels: list[int]) -> bytes:
    """Firmware-side pack: 2 taxels per 3 bytes (mirror of the unpack)."""
    out = bytearray()
    for k in range((len(taxels) + 1) // 2):
        a = taxels[2 * k] & 0x0FFF
        b = (taxels[2 * k + 1] & 0x0FFF) if (2 * k + 1) < len(taxels) else 0
        out.append(a >> 4)
        out.append(((a & 0x0F) << 4) | (b >> 8))
        out.append(b & 0xFF)
    return bytes(out)


def _build_packet(
    samples: list[tuple[int, list[int], tuple[int, ...]]],
    *,
    flags: int = FLAG_PACKED,
    seq_base: int = 1000,
    t_base_us: int = 5_000_000,
    trailing_imu: bool = True,
) -> bytes:
    """Build a packed12 v5 packet from ``(dt_us, taxels, imu)`` tuples."""
    payload = bytearray(struct.pack("<BBII", len(samples), flags, seq_base, t_base_us))
    for idx, (dt_us, taxels, imu) in enumerate(samples):
        payload += struct.pack("<H", dt_us)
        payload += _pack_taxels12(taxels)
        is_last = idx == len(samples) - 1
        if trailing_imu or not is_last:
            payload += struct.pack("<6h", *imu)
    return bytes(payload)


def _ramp(n: int = 80) -> list[int]:
    """A deterministic 0..4095 taxel ramp that exercises 12-bit boundaries."""
    return [(i * 51) & 0x0FFF for i in range(n)]


def test_unpack_taxels12_even_odd_boundary():
    # a=0x0AB (even), b=0xCDE (odd) → b0=0x0A, b1=0xBC, b2=0xDE
    packed = bytes([0x0A, 0xBC, 0xDE])
    assert unpack_taxels12(packed, 2) == (0x0AB, 0xCDE)


def test_unpack_taxels12_full_range_roundtrip():
    taxels = _ramp(80)
    assert unpack_taxels12(_pack_taxels12(taxels), 80) == tuple(taxels)


def test_parse_well_formed_batch():
    imu0 = (1, 2, 3, -4, -5, -6)
    imu1 = (7, 8, 9, -10, -11, -12)
    imu2 = (13, 14, 15, -16, -17, -18)
    pkt = _build_packet(
        [
            (0, _ramp(), imu0),
            (10_000, _ramp(), imu1),
            (20_000, _ramp(), imu2),
        ],
        seq_base=1000,
        t_base_us=5_000_000,
    )
    parsed = parse_v5(pkt)

    assert parsed.count == 3
    assert parsed.seq_base == 1000
    assert parsed.t_base_us == 5_000_000
    assert [s.seq for s in parsed.samples] == [1000, 1001, 1002]
    assert [s.device_us for s in parsed.samples] == [5_000_000, 5_010_000, 5_020_000]
    assert parsed.samples[0].device_ns == 5_000_000_000
    assert parsed.samples[0].taxels == tuple(_ramp())
    assert parsed.samples[0].imu == imu0
    assert parsed.samples[2].imu == imu2


def test_parse_default_stride_matches_constants():
    assert SAMPLE_STRIDE == 134
    pkt = _build_packet([(0, _ramp(), (0, 0, 0, 0, 0, 0))])
    # header + one full slot
    assert len(pkt) == HEADER_LEN + SAMPLE_STRIDE


def test_truncated_trailing_imu_keeps_taxels():
    pkt = _build_packet(
        [(0, _ramp(), (0, 0, 0, 0, 0, 0))],
        trailing_imu=False,
    )
    parsed = parse_v5(pkt)
    assert parsed.count == 1
    assert parsed.samples[0].taxels == tuple(_ramp())
    assert parsed.samples[0].imu is None


def test_non_packed_flags_raise():
    pkt = _build_packet([(0, _ramp(), (0, 0, 0, 0, 0, 0))], flags=0x02)
    with pytest.raises(OgloProtocolError, match="unsupported framing"):
        parse_v5(pkt)


def test_short_header_raises():
    with pytest.raises(OgloProtocolError, match="short packet"):
        parse_v5(b"\x01\x04\x00")


def test_truncated_taxels_raise():
    pkt = _build_packet([(0, _ramp(), (0, 0, 0, 0, 0, 0))])
    # Chop into the taxel block of the (only) sample.
    with pytest.raises(OgloProtocolError, match="truncated"):
        parse_v5(pkt[: HEADER_LEN + 40])


def test_imu_raw_len_constant():
    assert IMU_RAW_LEN == 12
