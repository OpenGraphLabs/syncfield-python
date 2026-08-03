"""Strict parser tests for OVISION's MJPEG-embedded YCTC metadata."""

from __future__ import annotations

import struct

import pytest

from syncfield.adapters.ovision_metadata import (
    ACCEL_M_S2_PER_LSB,
    GYRO_RAD_S_PER_LSB,
    OvisionMetadataError,
    extract_yctc_app15,
    extract_yctc_h264_sei,
    parse_ovision_h264_metadata,
    parse_ovision_mjpeg_metadata,
    parse_yctc_payload,
)


def _payload(*, count_a: int = 2, count_b: int = 2, count_low: int = 1) -> bytes:
    imu_len = 8 + 18 * (count_a + count_b)
    mag_len = 8 + 28 * count_low
    size = 72 + imu_len + mag_len
    out = bytearray(size)
    struct.pack_into(
        "<4sHHQQIIIIQQIIBBBBHH",
        out,
        0,
        b"YCTC",
        2,
        size,
        1_000_000,
        1_000_000,
        10_003,
        10_013,
        29,
        29,
        989_600,
        989_609,
        1,
        1,
        0x1F,
        0x1F,
        0,
        1,
        imu_len,
        mag_len,
    )
    offset = 72
    struct.pack_into(
        "<IHH",
        out,
        offset,
        1,
        count_a,
        count_b,
    )
    offset += 8
    for i in range(count_a):
        struct.pack_into("<hhhIQ", out, offset, -40 + i, -20, 5, 35_988, 970_000 + 2_000 * i)
        offset += 18
    for i in range(count_b):
        struct.pack_into("<hhhIQ", out, offset, -3_580, -240 + i, -7_200, 35_988, 970_000 + 2_000 * i)
        offset += 18
    struct.pack_into("<IHH", out, offset, 1, count_low, 0)
    offset += 8
    for i in range(count_low):
        struct.pack_into("<iiiB3xiQ", out, offset, -471, -1_941, 6_299, 255, 0, 975_000 + 10_000 * i)
        offset += 28
    assert offset == size
    return bytes(out)


def _jpeg(payload: bytes) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 4) + b"JF"
    app15 = b"\xff\xef" + struct.pack(">H", len(payload) + 2) + payload
    return b"\xff\xd8" + app0 + app15 + b"\xff\xda" + b"entropy\xff\xd9"


def _escape_h264_rbsp(data: bytes) -> bytes:
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _sei_message(payload_type: int, payload: bytes) -> bytes:
    header = bytearray()
    while payload_type >= 255:
        header.append(255)
        payload_type -= 255
    header.append(payload_type)
    size = len(payload)
    while size >= 255:
        header.append(255)
        size -= 255
    header.append(size)
    return bytes(header) + payload


def _h264(payload: bytes) -> bytes:
    # One unrelated SEI proves that selection is by YCTC magic, not position.
    rbsp = _sei_message(5, b"encoder") + _sei_message(240, payload) + b"\x80"
    sei = b"\x00\x00\x00\x01\x06" + _escape_h264_rbsp(rbsp)
    idr = b"\x00\x00\x01\x65encoded"
    return sei + idr


def test_extract_and_parse_yctc_metadata():
    parsed = parse_ovision_mjpeg_metadata(_jpeg(_payload()))

    assert parsed.version == 2
    assert parsed.left_start_line_rx_pts_us == parsed.right_start_line_rx_pts_us == 1_000_000
    assert parsed.left_gpio_trigger_index == parsed.right_gpio_trigger_index == 29
    assert parsed.stereo_exposure_start_skew_us == 9
    assert parsed.user_data_seq == 1
    assert parsed.frame_meta_generation == 1
    assert parsed.imu_generation == 1
    assert [s.xyz_raw for s in parsed.gyro] == [(-40, -20, 5), (-39, -20, 5)]
    assert parsed.accel[0].xyz_raw == (-3_580, -240, -7_200)
    assert parsed.gyro[1].device_timestamp_us == 972_000
    assert parsed.mag_generation == 1
    assert parsed.mag[0].xyz_raw == (-471, -1_941, 6_299)
    assert parsed.mag[0].tout_raw == 255
    assert parsed.mag[0].device_timestamp_us == 975_000
    assert parsed.gyro[0].gyro_rad_s()[0] == pytest.approx(-40 * GYRO_RAD_S_PER_LSB)
    assert parsed.accel[0].accel_m_s2()[0] == pytest.approx(-3_580 * ACCEL_M_S2_PER_LSB)


def test_extract_and_parse_h264_yctc_sei():
    payload = _payload(count_a=17, count_b=17, count_low=4)
    access_unit = _h264(payload)

    assert extract_yctc_h264_sei(access_unit) == payload
    parsed = parse_ovision_h264_metadata(access_unit)
    assert parsed.payload_size == 812
    assert len(parsed.gyro) == 17
    assert len(parsed.accel) == 17
    assert len(parsed.mag) == 4


def test_h264_parser_rejects_missing_and_truncated_sei():
    with pytest.raises(OvisionMetadataError, match="SEI NAL missing"):
        extract_yctc_h264_sei(b"\x00\x00\x01\x65encoded")
    with pytest.raises(OvisionMetadataError, match="truncated"):
        extract_yctc_h264_sei(b"\x00\x00\x01\x06\xf0\xff")


@pytest.mark.parametrize("counts", [(16, 16, 3), (16, 16, 4), (17, 17, 3), (17, 17, 4)])
def test_real_device_sample_count_shapes_are_accepted(counts):
    a, b, low = counts
    parsed = parse_yctc_payload(_payload(count_a=a, count_b=b, count_low=low))
    assert len(parsed.gyro) == a
    assert len(parsed.accel) == b
    assert len(parsed.mag) == low


def test_extract_rejects_non_jpeg_and_missing_metadata():
    with pytest.raises(OvisionMetadataError, match="SOI"):
        extract_yctc_app15(b"not jpeg")
    with pytest.raises(OvisionMetadataError, match="missing"):
        extract_yctc_app15(b"\xff\xd8\xff\xdaentropy\xff\xd9")


def test_parser_rejects_size_version_and_stereo_mismatch():
    bad_size = bytearray(_payload())
    struct.pack_into("<H", bad_size, 6, len(bad_size) + 1)
    with pytest.raises(OvisionMetadataError, match="size mismatch"):
        parse_yctc_payload(bytes(bad_size))

    bad_version = bytearray(_payload())
    struct.pack_into("<H", bad_version, 4, 3)
    with pytest.raises(OvisionMetadataError, match="unsupported"):
        parse_yctc_payload(bytes(bad_version))

    bad_counter = bytearray(_payload())
    struct.pack_into("<I", bad_counter, 36, 30)
    with pytest.raises(OvisionMetadataError, match="trigger indices differ"):
        parse_yctc_payload(bytes(bad_counter))


def test_parser_rejects_truncated_sample_blocks():
    payload = bytearray(_payload())
    payload.pop()
    struct.pack_into("<H", payload, 6, len(payload))
    with pytest.raises(OvisionMetadataError, match="lengths do not consume"):
        parse_yctc_payload(bytes(payload))


def test_parser_rejects_invalid_flags_and_reserved_bytes():
    bad_flags = bytearray(_payload())
    bad_flags[64] = 0x80
    with pytest.raises(OvisionMetadataError, match="unknown validity flags"):
        parse_yctc_payload(bytes(bad_flags))

    bad_reserved = bytearray(_payload())
    imu_len = struct.unpack_from("<H", bad_reserved, 68)[0]
    mag_sample_offset = 72 + imu_len + 8
    bad_reserved[mag_sample_offset + 13] = 1
    with pytest.raises(OvisionMetadataError, match="reserved bytes"):
        parse_yctc_payload(bytes(bad_reserved))
