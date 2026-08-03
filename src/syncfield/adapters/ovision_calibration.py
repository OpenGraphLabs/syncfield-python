"""Read and validate per-unit OVISION calibration over the Linux UVC XU.

This is a narrow implementation of HAMPO's V3 customer protocol.  It exposes
only read-only calibration and output-mode queries; firmware recovery and
control mutation deliberately remain outside the capture adapter.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


XU_UNIT_ID = 0x0A
SELECTOR_CALIB_INFO = 0x02
SELECTOR_CALIB_TRANSFER = 0x03
SELECTOR_CALIB_COMMAND = 0x04
SELECTOR_OUTPUT_MODE = 0x06
UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81


class OvisionCalibrationError(RuntimeError):
    """Per-unit calibration could not be read or failed integrity checks."""


@dataclass(frozen=True)
class OvisionCalibration:
    blob: bytes
    yaml_text: str
    serial_number: str
    schema_version: int
    calibration_version: int
    blob_crc32: int
    payload_sha256: str
    output_mode: str

    def parsed_yaml(self) -> dict[str, Any]:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional extra guard
            raise OvisionCalibrationError(
                "OVISION calibration parsing requires PyYAML"
            ) from exc
        data = yaml.safe_load(self.yaml_text)
        if not isinstance(data, dict):
            raise OvisionCalibrationError("Kalibr payload is not a mapping")
        validate_kalibr_camchain(data)
        return data

    def capture_document(
        self,
        *,
        usb_serial: str | None = None,
        native_eye_resolution: tuple[int, int] = (1920, 1080),
    ) -> dict[str, Any]:
        """Build the lossless production sidecar used by downstream VIO."""

        data = self.parsed_yaml()
        streams: dict[str, Any] = {}
        for source_name, target_name in (("cam0", "left"), ("cam1", "right")):
            camera = data[source_name]
            source_resolution = tuple(int(v) for v in camera["resolution"])
            sx = native_eye_resolution[0] / source_resolution[0]
            sy = native_eye_resolution[1] / source_resolution[1]
            fx, fy, cx, cy = (float(v) for v in camera["intrinsics"])
            streams[target_name] = {
                "socket": source_name,
                "camera_model": camera["camera_model"],
                "distortion_model": camera["distortion_model"],
                "distortion_coeffs": camera["distortion_coeffs"],
                "resolution": list(native_eye_resolution),
                "calibration_resolution": list(source_resolution),
                "intrinsics": [
                    [fx * sx, 0.0, cx * sx],
                    [0.0, fy * sy, cy * sy],
                    [0.0, 0.0, 1.0],
                ],
                "T_cam_imu": camera["T_cam_imu"],
                "timeshift_cam_imu_s": float(camera["timeshift_cam_imu"]),
            }
        # Legacy consumers call the primary pinhole stream ``rgb``.  OVISION's
        # packed primary is not itself a pinhole image, so explicitly alias the
        # left eye rather than inventing intrinsics for 3840x1080.
        streams["rgb"] = {**streams["left"], "alias_of": "left"}

        return {
            "schema": "syncfield.ovision_calibration.v1",
            "device": {
                "name": "OVISION HAMPO-USB-3290-V1.0",
                "usb_serial": usb_serial,
                "calibration_serial": self.serial_number,
                "calibration_schema_version": self.schema_version,
                "calibration_version": self.calibration_version,
                "calibration_blob_crc32": self.blob_crc32,
                "calibration_payload_sha256": self.payload_sha256,
                "output_mode": self.output_mode,
            },
            "stereo": {
                "layout": "side_by_side",
                "packed_resolution": [native_eye_resolution[0] * 2, native_eye_resolution[1]],
                "eye_order": ["left", "right"],
                "T_right_left": data["cam1"]["T_cn_cnm1"],
                "synchronization": "internal_fsync",
            },
            "streams": streams,
            "imu": {
                "model": "TDK ICM-42688-P",
                "sample_rate_hz": 500,
                "accelerometer_range_g": 4,
                "accelerometer_lsb_per_g": 8192,
                "gyroscope_range_dps": 1000,
                "gyroscope_lsb_per_dps": 32.768,
                "accelerometer_noise_density_m_s2_sqrt_hz": 70e-6 * 9.80665,
                "gyroscope_noise_density_rad_s_sqrt_hz": 2.8e-3 * 3.141592653589793 / 180.0,
                "intrinsic_source": "sensor_datasheet_not_per_unit_bias_calibration",
            },
            "magnetometer": {
                "model": "MEMSIC MMC5633NJL",
                "sample_rate_hz": 100,
                "raw_preserved": True,
                "tesla_per_lsb_assuming_20_bit": 1e-4 / 16384.0,
                "scale_status": "datasheet_mode_inferred_not_firmware_declared",
            },
            "raw_kalibr_yaml": self.yaml_text,
        }


class _UvcXuControlQuery(ctypes.Structure):
    _fields_ = [
        ("unit", ctypes.c_uint8),
        ("selector", ctypes.c_uint8),
        ("query", ctypes.c_uint8),
        ("size", ctypes.c_uint16),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
    ]


def _uvcioc_ctrl_query() -> int:
    # Linux _IOWR('u', 0x21, struct uvc_xu_control_query).
    return (3 << 30) | (ord("u") << 8) | 0x21 | (ctypes.sizeof(_UvcXuControlQuery) << 16)


def _xu_query(path: str | Path, selector: int, query: int, size: int, payload: bytes | None = None) -> bytes:
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - non-Linux
        raise OvisionCalibrationError("OVISION XU readback requires Linux") from exc
    buf = (ctypes.c_uint8 * max(size, 1))()
    if payload is not None:
        if len(payload) != size:
            raise OvisionCalibrationError("XU payload length mismatch")
        for index, value in enumerate(payload):
            buf[index] = value
    control = _UvcXuControlQuery(XU_UNIT_ID, selector, query, size, buf)
    fd = os.open(os.fspath(path), os.O_RDWR)
    try:
        fcntl.ioctl(fd, _uvcioc_ctrl_query(), control, True)
    except OSError as exc:
        raise OvisionCalibrationError(
            f"UVC XU query selector 0x{selector:02x} failed on {path}: {exc}"
        ) from exc
    finally:
        os.close(fd)
    return bytes(buf[:size])


def _crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def validate_kalibr_camchain(data: dict[str, Any]) -> None:
    for camera_name in ("cam0", "cam1"):
        camera = data.get(camera_name)
        if not isinstance(camera, dict):
            raise OvisionCalibrationError(f"{camera_name} missing from Kalibr payload")
        for field in (
            "T_cam_imu", "camera_model", "distortion_model",
            "distortion_coeffs", "intrinsics", "resolution", "timeshift_cam_imu",
        ):
            if field not in camera:
                raise OvisionCalibrationError(f"{camera_name}.{field} missing")
        if len(camera["intrinsics"]) != 4 or len(camera["resolution"]) != 2:
            raise OvisionCalibrationError(f"{camera_name} intrinsics/resolution malformed")
        matrix = camera["T_cam_imu"]
        if len(matrix) != 4 or any(len(row) != 4 for row in matrix):
            raise OvisionCalibrationError(f"{camera_name}.T_cam_imu malformed")
    baseline = data["cam1"].get("T_cn_cnm1")
    if not isinstance(baseline, list) or len(baseline) != 4 or any(len(row) != 4 for row in baseline):
        raise OvisionCalibrationError("cam1.T_cn_cnm1 missing or malformed")


def read_ovision_calibration(video_device: str | Path = "/dev/video0") -> OvisionCalibration:
    """Read, CRC-check and parse the active per-unit schema-v2 blob."""

    info_raw = _xu_query(video_device, SELECTOR_CALIB_INFO, UVC_GET_CUR, 16)
    schema, version, expected_crc, total_length = struct.unpack_from("<HHII", info_raw)
    if info_raw[12] != 0 or not 68 <= total_length <= 8192:
        raise OvisionCalibrationError("device reports no healthy calibration blob")
    blob = bytearray()
    session_id = os.getpid() & 0xFFFFFFFF or 1
    for offset in range(0, total_length, 48):
        size = min(48, total_length - offset)
        command = struct.pack("<BBHIII", 0x05, 0, 0, session_id, offset, size)
        _xu_query(video_device, SELECTOR_CALIB_COMMAND, UVC_SET_CUR, 16, command)
        transfer = _xu_query(video_device, SELECTOR_CALIB_TRANSFER, UVC_GET_CUR, 60)
        got_session, got_offset, got_size, chunk_crc = struct.unpack_from("<IIHH", transfer)
        chunk = transfer[12 : 12 + got_size]
        if (got_session, got_offset, got_size) != (session_id, offset, size):
            raise OvisionCalibrationError("calibration chunk identity mismatch")
        if _crc16_modbus(chunk) != chunk_crc:
            raise OvisionCalibrationError("calibration chunk CRC16 mismatch")
        blob.extend(chunk)
    blob_bytes = bytes(blob)
    if zlib.crc32(blob_bytes) & 0xFFFFFFFF != expected_crc:
        raise OvisionCalibrationError("complete calibration CRC32 mismatch")
    if blob_bytes[:4] != b"ZXCZ" or schema != 2 or struct.unpack_from("<H", blob_bytes, 4)[0] != 2:
        raise OvisionCalibrationError("camera does not contain schema-v2 Kalibr calibration")
    payload_length = struct.unpack_from("<I", blob_bytes, 8)[0]
    if len(blob_bytes) != 64 + payload_length + 4:
        raise OvisionCalibrationError("internal calibration blob length mismatch")
    if zlib.crc32(blob_bytes[:-4]) & 0xFFFFFFFF != struct.unpack_from("<I", blob_bytes, len(blob_bytes) - 4)[0]:
        raise OvisionCalibrationError("internal calibration tail CRC32 mismatch")
    serial = blob_bytes[12:44].split(b"\x00", 1)[0].decode("ascii")
    payload = blob_bytes[64:-4]
    try:
        yaml_text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OvisionCalibrationError("calibration payload is not UTF-8 YAML") from exc
    required = ("cam0:", "cam1:", "T_cam_imu:", "T_cn_cnm1:", "timeshift_cam_imu:")
    if any(token not in yaml_text for token in required):
        raise OvisionCalibrationError("calibration YAML is missing required Kalibr fields")
    mode_raw = _xu_query(video_device, SELECTOR_OUTPUT_MODE, UVC_GET_CUR, 1)
    mode = {1: "internal", 2: "external"}.get(mode_raw[0], f"unknown:{mode_raw[0]}")
    return OvisionCalibration(
        blob_bytes, yaml_text, serial, schema, version, expected_crc,
        hashlib.sha256(payload).hexdigest(), mode,
    )
