"""Parse HAMPO/ZXCZ ``YCTC`` V2 metadata embedded by OVISION cameras.

The SC233HGS stereo module transports frame timing, ICM-42688-P gyro and
accelerometer samples, and MMC5633NJL magnetometer samples in each compressed
video frame.  This module follows the manufacturer's V3 wire protocol.  All
timestamps share the camera's monotonic microsecond clock.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass


YCTC_MAGIC = b"YCTC"
YCTC_VERSION = 2
YCTC_V2_HEADER_SIZE = 72

SENSOR_PRESENT = 0x01
START_LINE_RX_PTS_VALID = 0x02
EXPOSURE_START_PTS_VALID = 0x04
EXPOSURE_TIME_VALID = 0x08
GPIO_TRIGGER_INDEX_VALID = 0x10
_KNOWN_SENSOR_FLAGS = 0x1F

IMU_MAX_SAMPLES = 128
MAG_MAX_SAMPLES = 16
_IMU_HEADER_SIZE = 8
_IMU_SAMPLE_SIZE = 18
_MAG_HEADER_SIZE = 8
_MAG_SAMPLE_SIZE = 28

# Vendor-stated firmware configuration: accel +/-4 g, gyro +/-1000 dps.
STANDARD_GRAVITY_M_S2 = 9.80665
ACCEL_M_S2_PER_LSB = 4.0 * STANDARD_GRAVITY_M_S2 / 32768.0
GYRO_RAD_S_PER_LSB = math.radians(1000.0) / 32768.0

# MMC5633NJL 20-bit sensitivity.  The wire protocol does not identify the
# configured resolution, so production artifacts must retain xyz_raw even
# when this convenience conversion is used.
MAG_TESLA_PER_LSB_20_BIT = 1e-4 / 16384.0


class OvisionMetadataError(ValueError):
    """The compressed frame contains no valid, complete OVISION metadata."""


@dataclass(frozen=True)
class OvisionImuSample:
    """One gyro or accelerometer sample in the IMU sensor frame."""

    xyz_raw: tuple[int, int, int]
    temperature_raw: int
    device_timestamp_us: int

    def gyro_rad_s(self) -> tuple[float, float, float]:
        return tuple(value * GYRO_RAD_S_PER_LSB for value in self.xyz_raw)

    def accel_m_s2(self) -> tuple[float, float, float]:
        return tuple(value * ACCEL_M_S2_PER_LSB for value in self.xyz_raw)


@dataclass(frozen=True)
class OvisionMagSample:
    """One MMC5633NJL sample in the magnetometer sensor frame."""

    xyz_raw: tuple[int, int, int]
    tout_raw: int
    temperature_milli_c: int
    device_timestamp_us: int

    def tesla_20_bit(self) -> tuple[float, float, float]:
        """Convert assuming the firmware's empirically identified 20-bit mode."""

        return tuple(value * MAG_TESLA_PER_LSB_20_BIT for value in self.xyz_raw)


@dataclass(frozen=True)
class OvisionFrameMetadata:
    """Validated V2 timing and sensor batches from one video frame."""

    version: int
    payload_size: int
    left_start_line_rx_pts_us: int
    right_start_line_rx_pts_us: int
    left_exposure_time_us: int
    right_exposure_time_us: int
    left_gpio_trigger_index: int
    right_gpio_trigger_index: int
    left_exposure_start_pts_us: int
    right_exposure_start_pts_us: int
    user_data_seq: int
    frame_meta_generation: int
    left_valid_flags: int
    right_valid_flags: int
    left_vi_pipe: int
    right_vi_pipe: int
    imu_generation: int | None
    gyro: tuple[OvisionImuSample, ...]
    accel: tuple[OvisionImuSample, ...]
    mag_generation: int | None
    mag: tuple[OvisionMagSample, ...]

    @property
    def stereo_exposure_start_skew_us(self) -> int:
        """Signed right-minus-left exposure-start skew."""

        return self.right_exposure_start_pts_us - self.left_exposure_start_pts_us


def extract_yctc_app15(jpeg: bytes) -> bytes:
    """Return the ``YCTC`` APP15 payload from a complete JPEG frame."""

    if len(jpeg) < 4 or jpeg[:2] != b"\xff\xd8":
        raise OvisionMetadataError("not a JPEG SOI stream")

    offset = 2
    while offset + 2 <= len(jpeg):
        if jpeg[offset] != 0xFF:
            raise OvisionMetadataError(f"invalid JPEG marker prefix at byte {offset}")
        marker = jpeg[offset + 1]
        offset += 2
        if marker == 0xDA:
            break
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(jpeg):
            raise OvisionMetadataError("truncated JPEG segment length")
        segment_size = int.from_bytes(jpeg[offset : offset + 2], "big")
        if segment_size < 2:
            raise OvisionMetadataError("invalid JPEG segment length")
        end = offset + segment_size
        if end > len(jpeg):
            raise OvisionMetadataError("truncated JPEG segment")
        payload = jpeg[offset + 2 : end]
        if marker == 0xEF and payload.startswith(YCTC_MAGIC):
            return payload
        offset = end
    raise OvisionMetadataError("YCTC APP15 segment missing")


def _annex_b_nalus(data: bytes):
    starts: list[tuple[int, int]] = []
    offset = 0
    while offset + 3 <= len(data):
        if data.startswith(b"\x00\x00\x00\x01", offset):
            starts.append((offset, 4))
            offset += 4
        elif data.startswith(b"\x00\x00\x01", offset):
            starts.append((offset, 3))
            offset += 3
        else:
            offset += 1
    for index, (start, prefix_size) in enumerate(starts):
        body_start = start + prefix_size
        body_end = starts[index + 1][0] if index + 1 < len(starts) else len(data)
        if body_end > body_start:
            yield data[body_start:body_end]


def _unescape_h264_rbsp(escaped: bytes) -> bytes:
    out = bytearray()
    zero_count = 0
    for byte in escaped:
        if zero_count >= 2 and byte == 0x03:
            # Count encoded bytes. The skipped byte breaks the encoded zero run.
            zero_count = 0
            continue
        out.append(byte)
        zero_count = zero_count + 1 if byte == 0 else 0
    return bytes(out)


def extract_yctc_h264_sei(access_unit: bytes) -> bytes:
    """Return the ``YCTC`` message from an Annex-B H.264 access unit."""

    saw_sei = False
    for nal in _annex_b_nalus(access_unit):
        if not nal or nal[0] & 0x1F != 6:
            continue
        saw_sei = True
        rbsp = _unescape_h264_rbsp(nal[1:])
        offset = 0
        while offset < len(rbsp):
            if rbsp[offset] == 0x80 and offset + 1 == len(rbsp):
                break
            payload_type = 0
            while offset < len(rbsp) and rbsp[offset] == 0xFF:
                payload_type += 0xFF
                offset += 1
            if offset >= len(rbsp):
                raise OvisionMetadataError("truncated H.264 SEI payload type")
            payload_type += rbsp[offset]
            offset += 1
            payload_size = 0
            while offset < len(rbsp) and rbsp[offset] == 0xFF:
                payload_size += 0xFF
                offset += 1
            if offset >= len(rbsp):
                raise OvisionMetadataError("truncated H.264 SEI payload size")
            payload_size += rbsp[offset]
            offset += 1
            end = offset + payload_size
            if end > len(rbsp):
                raise OvisionMetadataError(f"truncated H.264 SEI payload type {payload_type}")
            payload = rbsp[offset:end]
            if payload.startswith(YCTC_MAGIC):
                return payload
            offset = end
    detail = "YCTC SEI message missing" if saw_sei else "H.264 SEI NAL missing"
    raise OvisionMetadataError(detail)


def _validate_sensor_fields(
    side: str,
    flags: int,
    vi_pipe: int,
    start_line_pts: int,
    exposure_start_pts: int,
    exposure_time: int,
    trigger_index: int,
) -> None:
    if flags & ~_KNOWN_SENSOR_FLAGS:
        raise OvisionMetadataError(f"{side} sensor has unknown validity flags 0x{flags:02x}")
    fields = (
        (START_LINE_RX_PTS_VALID, start_line_pts, "start-line timestamp"),
        (EXPOSURE_START_PTS_VALID, exposure_start_pts, "exposure-start timestamp"),
        (EXPOSURE_TIME_VALID, exposure_time, "exposure time"),
        (GPIO_TRIGGER_INDEX_VALID, trigger_index, "trigger index"),
    )
    for flag, value, name in fields:
        if not flags & flag and value != 0:
            raise OvisionMetadataError(f"{side} {name} is nonzero while invalid")
    if not flags & SENSOR_PRESENT and (vi_pipe != 0 or flags != 0):
        raise OvisionMetadataError(f"{side} absent sensor contains nonzero metadata")


def parse_yctc_payload(payload: bytes) -> OvisionFrameMetadata:
    """Strictly parse one manufacturer V3 ``YCTC`` V2 payload."""

    if len(payload) < YCTC_V2_HEADER_SIZE:
        raise OvisionMetadataError(
            f"YCTC payload too short: {len(payload)} < {YCTC_V2_HEADER_SIZE}"
        )
    if payload[:4] != YCTC_MAGIC:
        raise OvisionMetadataError("YCTC magic missing")

    version, declared_size = struct.unpack_from("<HH", payload, 4)
    if version != YCTC_VERSION:
        raise OvisionMetadataError(f"unsupported YCTC version {version}; expected {YCTC_VERSION}")
    if declared_size != len(payload):
        raise OvisionMetadataError(f"YCTC size mismatch: header={declared_size}, actual={len(payload)}")

    (
        left_start_line,
        right_start_line,
        left_exposure_time,
        right_exposure_time,
        left_trigger,
        right_trigger,
        left_exposure_start,
        right_exposure_start,
        user_data_seq,
        frame_meta_generation,
        left_flags,
        right_flags,
        left_pipe,
        right_pipe,
        imu_payload_len,
        mag_payload_len,
    ) = struct.unpack_from("<QQIIIIQQIIBBBBHH", payload, 8)

    if YCTC_V2_HEADER_SIZE + imu_payload_len + mag_payload_len != len(payload):
        raise OvisionMetadataError("YCTC IMU/MAG lengths do not consume payload")
    _validate_sensor_fields(
        "left", left_flags, left_pipe, left_start_line, left_exposure_start,
        left_exposure_time, left_trigger,
    )
    _validate_sensor_fields(
        "right", right_flags, right_pipe, right_start_line, right_exposure_start,
        right_exposure_time, right_trigger,
    )
    if left_flags & GPIO_TRIGGER_INDEX_VALID and right_flags & GPIO_TRIGGER_INDEX_VALID:
        if left_trigger != right_trigger:
            raise OvisionMetadataError(
                f"stereo trigger indices differ: left={left_trigger}, right={right_trigger}"
            )

    offset = YCTC_V2_HEADER_SIZE
    imu_generation: int | None = None
    gyro: list[OvisionImuSample] = []
    accel: list[OvisionImuSample] = []
    if imu_payload_len:
        if imu_payload_len < _IMU_HEADER_SIZE:
            raise OvisionMetadataError("truncated YCTC IMU header")
        imu_end = offset + imu_payload_len
        imu_generation, gyro_count, accel_count = struct.unpack_from("<IHH", payload, offset)
        if gyro_count > IMU_MAX_SAMPLES or accel_count > IMU_MAX_SAMPLES:
            raise OvisionMetadataError("YCTC IMU sample count exceeds protocol maximum")
        if _IMU_HEADER_SIZE + (gyro_count + accel_count) * _IMU_SAMPLE_SIZE != imu_payload_len:
            raise OvisionMetadataError("YCTC IMU counts do not consume IMU payload")
        offset += _IMU_HEADER_SIZE

        def read_imu(count: int) -> list[OvisionImuSample]:
            nonlocal offset
            samples: list[OvisionImuSample] = []
            for _ in range(count):
                x, y, z, temperature, timestamp_us = struct.unpack_from("<hhhiQ", payload, offset)
                offset += _IMU_SAMPLE_SIZE
                samples.append(OvisionImuSample((x, y, z), temperature, timestamp_us))
            return samples

        gyro = read_imu(gyro_count)
        accel = read_imu(accel_count)
        if offset != imu_end:
            raise OvisionMetadataError("YCTC IMU parser did not consume payload")

    mag_generation: int | None = None
    mag: list[OvisionMagSample] = []
    if mag_payload_len:
        if mag_payload_len < _MAG_HEADER_SIZE:
            raise OvisionMetadataError("truncated YCTC MAG header")
        mag_end = offset + mag_payload_len
        mag_generation, mag_count, reserved = struct.unpack_from("<IHH", payload, offset)
        if reserved != 0:
            raise OvisionMetadataError("YCTC MAG header reserved field is nonzero")
        if mag_count > MAG_MAX_SAMPLES:
            raise OvisionMetadataError("YCTC MAG sample count exceeds protocol maximum")
        if _MAG_HEADER_SIZE + mag_count * _MAG_SAMPLE_SIZE != mag_payload_len:
            raise OvisionMetadataError("YCTC MAG count does not consume MAG payload")
        offset += _MAG_HEADER_SIZE
        for _ in range(mag_count):
            x, y, z, tout = struct.unpack_from("<iiiB", payload, offset)
            if payload[offset + 13 : offset + 16] != b"\x00\x00\x00":
                raise OvisionMetadataError("YCTC MAG sample reserved bytes are nonzero")
            temperature, timestamp_us = struct.unpack_from("<iQ", payload, offset + 16)
            offset += _MAG_SAMPLE_SIZE
            mag.append(OvisionMagSample((x, y, z), tout, temperature, timestamp_us))
        if offset != mag_end:
            raise OvisionMetadataError("YCTC MAG parser did not consume payload")

    if offset != len(payload):
        raise OvisionMetadataError("YCTC parser did not consume payload")

    return OvisionFrameMetadata(
        version, declared_size, left_start_line, right_start_line,
        left_exposure_time, right_exposure_time, left_trigger, right_trigger,
        left_exposure_start, right_exposure_start, user_data_seq,
        frame_meta_generation, left_flags, right_flags, left_pipe, right_pipe,
        imu_generation, tuple(gyro), tuple(accel), mag_generation, tuple(mag),
    )


def parse_ovision_mjpeg_metadata(jpeg: bytes) -> OvisionFrameMetadata:
    return parse_yctc_payload(extract_yctc_app15(jpeg))


def parse_ovision_h264_metadata(access_unit: bytes) -> OvisionFrameMetadata:
    return parse_yctc_payload(extract_yctc_h264_sei(access_unit))
