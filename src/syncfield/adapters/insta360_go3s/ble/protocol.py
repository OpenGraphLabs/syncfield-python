"""Insta360 Go2BlePacket (FFFrame) protocol.

Ported verbatim from:
  syncfield_recorder/sensors/insta360_ble/protocol.py

Which was reverse-engineered from xaionaro-go/insta360ctl and validated
on 3x Insta360 GO 3S (firmware v8.0.4.11).

Ref: github.com/xaionaro-go/insta360ctl/doc/ble_protocol.md
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# BLE GATT UUIDs
SERVICE_UUID = "0000be80-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000be81-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000be82-0000-1000-8000-00805f9b34fb"

# FFFrame constants
FF_MARKER = 0xFF
TYPE_APP_TO_CAM = 0x07
TYPE_CAM_TO_APP = 0x06
SUBTYPE_MESSAGE = 0x40
SUBTYPE_SYNC = 0x41

# Command codes (phone -> camera)
CMD_TAKE_PICTURE = 0x0003
CMD_START_CAPTURE = 0x0004
CMD_STOP_CAPTURE = 0x0005
CMD_SET_OPTIONS = 0x0002
CMD_CHECK_AUTH = 0x0027
CMD_REQUEST_AUTH = 0x0056

# Response status codes
STATUS_OK = 0x00C8
STATUS_BAD_REQUEST = 0x0190
STATUS_ERROR = 0x01F4
STATUS_NOT_IMPL = 0x01F5

# Outer header size: FF_MARKER(1) + type(1) + subtype(1) + payload_len(2)
_OUTER_HDR_LEN = 5

# Inner header size (Go2BlePacket fixed header)
_INNER_HDR_LEN = 16


def crc16_modbus(data: bytes | bytearray) -> int:
    """CRC-16/MODBUS: polynomial 0xA001, init 0xFFFF, little-endian."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _append_crc(packet: bytes) -> bytes:
    return packet + struct.pack("<H", crc16_modbus(packet))


def build_sync_response() -> bytes:
    """Build SYNC response: FF 07 41 [len=7] [7 zero bytes] [CRC]."""
    header = struct.pack("<BBBH", FF_MARKER, TYPE_APP_TO_CAM, SUBTYPE_SYNC, 7)
    return _append_crc(header + bytes(7))


def build_command(cmd_code: int, seq: int, protobuf_payload: bytes = b"") -> bytes:
    """Build Go2BlePacket command with 16-byte inner header + optional protobuf."""
    inner_size = _INNER_HDR_LEN + len(protobuf_payload)
    hdr = bytearray(_INNER_HDR_LEN)
    struct.pack_into("<I", hdr, 0, inner_size)
    hdr[4] = 0x04  # mode = Message
    struct.pack_into("<H", hdr, 7, cmd_code)
    hdr[9] = 0x02  # content_type = protobuf
    hdr[10] = seq & 0xFF
    hdr[13] = 0x80  # is_last=1, direction=app->cam

    inner = bytes(hdr) + protobuf_payload
    outer = struct.pack("<BBB", FF_MARKER, TYPE_APP_TO_CAM, SUBTYPE_MESSAGE)
    outer += struct.pack("<H", len(inner))
    outer += inner
    return _append_crc(outer)


def build_message_packet(
    cmd: int, seq: int, protobuf_payload: bytes = b""
) -> bytes:
    """Build a Go2BlePacket command frame (public API alias for build_command).

    Args:
        cmd:              Command code (e.g. CMD_START_CAPTURE).
        seq:              Sequence counter (0–255, wraps).
        protobuf_payload: Optional serialised protobuf body.
    """
    return build_command(cmd_code=cmd, seq=seq, protobuf_payload=protobuf_payload)


def build_start_capture_pb(mode: int = 1) -> bytes:
    """Protobuf payload for CMD_START_CAPTURE with capture mode.

    Args:
        mode: 1 = INSCaptureModeNormal (standard video).
              iOS SDK: captureMode.mode = 1
    """
    # protobuf field 1, wire type 0 (varint), value = mode
    return bytes([0x08, mode & 0x7F])


def build_video_mode_options_pb() -> bytes:
    """Protobuf payload for CMD_SET_OPTIONS to force video normal mode.

    Sets videoSubMode=0 (Normal) via option type 41.
    iOS SDK: setOptions:forTypes: with INSCameraOptionsTypeVideoSubMode(41).
    """
    # Nested message: field 1 (varint) = type 41, field 2 (varint) = value 0
    return bytes([0x0A, 0x04, 0x08, 0x29, 0x10, 0x00])


def build_check_auth_pb(device_id: str) -> bytes:
    """Protobuf: field 1 (string) = auth_id, field 2 (varint) = 2 (APP)."""
    aid = device_id.encode("utf-8")
    return bytes([0x0A, len(aid)]) + aid + bytes([0x10, 0x02])


def build_check_auth_payload(addr: bytes | str) -> bytes:
    """Build the CheckAuth protobuf payload for a given device address.

    Accepts either a raw bytes address or a str (encoded as UTF-8).
    Public alias used by T04 and test suite.
    """
    if isinstance(addr, str):
        aid = addr.encode("utf-8")
    else:
        aid = addr
    return bytes([0x0A, len(aid)]) + aid + bytes([0x10, 0x02])


def build_heartbeat(seq: int, device_id: str) -> bytes:
    """Build a heartbeat packet using CheckAuth as keepalive.

    The iOS SDK sends SCMP HeartBeat (0x05) every 0.5s. CMD_CHECK_AUTH
    is proven to work on GO 3S without side effects and keeps the
    BLE connection alive.
    """
    pb = build_check_auth_pb(device_id)
    return build_command(CMD_CHECK_AUTH, seq, pb)


@dataclass
class BLEResponse:
    """Parsed BLE response from camera (recorder-compatible dataclass)."""

    raw: bytes
    is_sync: bool = False
    status_code: int = 0
    seq: int = 0
    video_path: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.status_code == STATUS_OK

    @property
    def is_error(self) -> bool:
        return self.status_code >= STATUS_BAD_REQUEST


@dataclass
class ParsedResponse:
    """Structured parse of a camera->app FFFrame response packet.

    The camera does NOT echo the request's cmd_code. Instead, the inner[7:9]
    slot that carries cmd_code in app->cam requests is repurposed to carry
    the status_code in cam->app responses. Correlate responses to requests
    purely by ``seq``.

    Attributes:
        seq:     Sequence number echoed from the request (inner[10]).
        status:  Status code (2 bytes LE at inner[7:9]; 0x00C8 = OK).
        payload: Protobuf payload past the 16-byte inner header.
    """

    seq: int
    status: int
    payload: bytes


@dataclass
class ParsedRequest:
    """Structured parse of an app->camera FFFrame request packet.

    Attributes:
        cmd:     Command code (2 bytes LE at inner[7:9]).
        seq:     Sequence number (1 byte at inner[10]).
        payload: Protobuf payload past the 16-byte inner header.
    """

    cmd: int
    seq: int
    payload: bytes


def parse_response(data: bytes) -> BLEResponse | None:
    """Parse a Go2BlePacket response from camera (recorder-compatible API)."""
    if len(data) < 3 or data[0] != FF_MARKER:
        return None

    if data[2] == SUBTYPE_SYNC:
        return BLEResponse(raw=data, is_sync=True)

    if data[2] == SUBTYPE_MESSAGE and len(data) >= 14:
        status_code = struct.unpack_from("<H", data, 12)[0]
        seq = data[15] if len(data) > 15 else 0

        video_path = None
        try:
            text = data.decode("ascii", errors="replace")
            if "/DCIM/" in text:
                start = text.index("/DCIM/")
                # Try .mp4 first, then .insv
                end = -1
                for ext in (".mp4", ".insv"):
                    try:
                        end = text.index(ext, start) + len(ext)
                        break
                    except ValueError:
                        continue
                if end > start:
                    video_path = text[start:end]
        except ValueError:
            pass

        return BLEResponse(
            raw=data, status_code=status_code, seq=seq, video_path=video_path
        )

    return BLEResponse(raw=data)


def parse_response_packet(pkt: bytes) -> ParsedResponse | None:
    """Parse a camera->app FFFrame packet into a structured ParsedResponse.

    FFFrame outer header layout:
      [0]     FF_MARKER (0xFF)
      [1]     frame type (0x06 = cam->app)
      [2]     subtype (0x40 = message, 0x41 = sync)
      [3:5]   payload length (LE uint16)
      [5:]    inner payload (Go2BlePacket header + protobuf)
      [-2:]   CRC-16/MODBUS over all preceding bytes

    Inner header layout for cam->app responses (16 bytes, at offset 5):
      [0:4]   total inner size (LE uint32)
      [4]     mode
      [5:7]   reserved
      [7:9]   status_code (LE uint16)   <-- reused slot; in app->cam this is cmd_code
      [9]     content_type
      [10]    seq
      [11:16] reserved / flags
      [16:]   protobuf payload

    Note: the camera does NOT echo the request cmd_code. Correlate by seq.
    This mirrors the recorder's validated ``parse_response`` byte offsets
    (``status_code`` at packet offset 12 = inner offset 7).

    Returns None if the packet is too short, has a bad marker, or is a SYNC.
    """
    if len(pkt) < _OUTER_HDR_LEN + _INNER_HDR_LEN + 2:
        return None
    if pkt[0] != FF_MARKER:
        return None
    if pkt[2] != SUBTYPE_MESSAGE:
        return None

    base = _OUTER_HDR_LEN  # inner header starts here
    status = struct.unpack_from("<H", pkt, base + 7)[0]
    seq = pkt[base + 10]
    payload = pkt[base + _INNER_HDR_LEN : -2]  # strip CRC trailer

    return ParsedResponse(seq=seq, status=status, payload=payload)


def parse_request_packet(pkt: bytes) -> ParsedRequest | None:
    """Parse an app->camera FFFrame request packet into a ParsedRequest.

    Uses the same outer + inner header layout as parse_response_packet,
    but expects frame type 0x07 (app->cam).

    Returns None if the packet is too short, has a bad marker, or is a SYNC.
    """
    if len(pkt) < _OUTER_HDR_LEN + _INNER_HDR_LEN + 2:
        return None
    if pkt[0] != FF_MARKER:
        return None
    if pkt[2] != SUBTYPE_MESSAGE:
        return None

    base = _OUTER_HDR_LEN
    cmd = struct.unpack_from("<H", pkt, base + 7)[0]
    seq = pkt[base + 10]
    payload = pkt[base + _INNER_HDR_LEN : -2]  # strip CRC trailer

    return ParsedRequest(cmd=cmd, seq=seq, payload=payload)
