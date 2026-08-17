"""OGLO schema-6 USB tagged-stream parser.

Each modality is independently framed and timestamped::

    TAG v1: A5 5A | type:u8 | payload_len:u16le | seq:u32le | t_us:u32le | payload
    TAG v2: A5 5B | type:u8 | payload_len:u16le | seq:u32le | t_us:u64le | payload

TAG v1 remains the firmware 0.9.10/0.9.12 compatibility path. TAG v2 first
ships in firmware 0.9.13 and removes the 71.6-minute device-clock rollover.
The parser accepts both versions so mixed fleets can be upgraded safely, while
the transport negotiates exactly one version per connection.

Only production schema-6 payload sizes are accepted. A bad header is discarded
up to the next known magic; a partial frame remains buffered.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator, Tuple

from syncfield.adapters.oglo.packet import OgloProtocolError, unpack_taxels12

__all__ = [
    "TAG_V1_MAGIC",
    "TAG_V2_MAGIC",
    "TAG_V1_HEADER_LEN",
    "TAG_V2_HEADER_LEN",
    "TAG_MAGIC",
    "TAG_HEADER_LEN",
    "TAG_TYPE_TACTILE",
    "TAG_TYPE_IMU",
    "TAG_TYPE_MAG",
    "UsbTaggedPacket",
    "parse_usb_packet",
    "iter_usb_packets",
    "STREAM_ON_COMMAND",
    "STREAM_OFF_COMMAND",
    "STREAM_V1_ON_COMMAND",
    "STREAM_V1_OFF_COMMAND",
    "STREAM_V2_ON_COMMAND",
    "STREAM_V2_OFF_COMMAND",
    "QUIET_COMMANDS",
]

TAG_V1_MAGIC = b"\xA5\x5A"
TAG_V2_MAGIC = b"\xA5\x5B"
TAG_V1_HEADER_LEN = 13
TAG_V2_HEADER_LEN = 17

# Backward-compatible aliases used by downstream tests and integrations.
TAG_MAGIC = TAG_V1_MAGIC
TAG_HEADER_LEN = TAG_V1_HEADER_LEN
TAG_TYPE_TACTILE = 1
TAG_TYPE_IMU = 2
TAG_TYPE_MAG = 3

_PAYLOAD_LENGTHS = {
    # 0.9.9+ uses native packed12 (120 B) to leave USB endpoint headroom.
    # Accept the earlier schema-6 widened form too so a rolling firmware update
    # cannot break collection while one glove is still on 0.9.8.
    TAG_TYPE_TACTILE: (120, 160),
    TAG_TYPE_IMU: (12,),
    TAG_TYPE_MAG: (6,),
}
_STRUCTS = {
    TAG_TYPE_TACTILE: struct.Struct("<80H"),
    TAG_TYPE_IMU: struct.Struct("<6h"),
    TAG_TYPE_MAG: struct.Struct("<3h"),
}

STREAM_V1_ON_COMMAND = b"STREAM TAG ON\n"
STREAM_V1_OFF_COMMAND = b"STREAM TAG OFF\n"
STREAM_V2_ON_COMMAND = b"STREAM TAG2 ON\n"
STREAM_V2_OFF_COMMAND = b"STREAM TAG2 OFF\n"

# Backward-compatible aliases: callers which do not negotiate explicitly stay
# on TAG v1 and therefore remain compatible with firmware 0.9.10/0.9.12.
STREAM_ON_COMMAND = STREAM_V1_ON_COMMAND
STREAM_OFF_COMMAND = STREAM_V1_OFF_COMMAND
QUIET_COMMANDS = (
    b"\nSTREAM TAG2 OFF\nSTREAM TAG OFF\nSTREAM BIN OFF\nSTREAM TAXEL OFF\n"
)


@dataclass(frozen=True)
class UsbTaggedPacket:
    stream_type: int
    seq: int
    device_us: int
    values: Tuple[int, ...]
    tag_version: int = 1

    @property
    def modality(self) -> str:
        return {TAG_TYPE_TACTILE: "tactile", TAG_TYPE_IMU: "imu", TAG_TYPE_MAG: "mag"}[self.stream_type]

    @property
    def device_ns(self) -> int:
        return self.device_us * 1_000


def parse_usb_packet(frame: bytes) -> UsbTaggedPacket:
    magic = frame[:2]
    if magic == TAG_V1_MAGIC:
        tag_version = 1
        header_len = TAG_V1_HEADER_LEN
        timestamp_format = "<II"
    elif magic == TAG_V2_MAGIC:
        tag_version = 2
        header_len = TAG_V2_HEADER_LEN
        timestamp_format = "<IQ"
    else:
        raise OgloProtocolError(f"bad USB tag magic: {frame[:2].hex()}")
    if len(frame) < header_len:
        raise OgloProtocolError(f"USB TAG v{tag_version} frame shorter than {header_len} bytes")
    stream_type = frame[2]
    payload_len = struct.unpack_from("<H", frame, 3)[0]
    expected_lengths = _payload_lengths(tag_version, stream_type)
    if expected_lengths is None:
        raise OgloProtocolError(f"unknown USB tag stream type {stream_type}")
    if payload_len not in expected_lengths:
        raise OgloProtocolError(
            f"USB tag {stream_type} payload must be one of {expected_lengths} bytes, got {payload_len}"
        )
    if len(frame) != header_len + payload_len:
        raise OgloProtocolError(
            f"USB TAG v{tag_version} frame must be {header_len + payload_len} bytes, "
            f"got {len(frame)}"
        )
    seq, device_us = struct.unpack_from(timestamp_format, frame, 5)
    if stream_type == TAG_TYPE_TACTILE and payload_len == 120:
        values = unpack_taxels12(frame[header_len:], 80)
    else:
        values = _STRUCTS[stream_type].unpack_from(frame, header_len)
    return UsbTaggedPacket(stream_type, seq, device_us, values, tag_version)


def _next_magic_index(view: bytes) -> int:
    indexes = tuple(
        index for index in (view.find(TAG_V1_MAGIC), view.find(TAG_V2_MAGIC))
        if index >= 0
    )
    return min(indexes) if indexes else -1


def _payload_lengths(tag_version: int, stream_type: int) -> tuple[int, ...] | None:
    """Exact lengths for one negotiated version.

    The 160-byte tactile compatibility form predates current packed12 firmware
    and is v1-only. TAG2 first ships with 0.9.13 and has exactly the same 120-byte
    packed tactile payload as current TAG v1.
    """
    if tag_version == 2 and stream_type == TAG_TYPE_TACTILE:
        return (120,)
    return _PAYLOAD_LENGTHS.get(stream_type)


def iter_usb_packets(buffer: bytes) -> tuple[Iterator[UsbTaggedPacket], bytes]:
    packets: list[UsbTaggedPacket] = []
    view = buffer
    while True:
        idx = _next_magic_index(view)
        if idx < 0:
            # Both magics start with A5, so one trailing prefix byte is enough
            # to recover a magic split across two serial reads.
            tail = view[-1:] if view.endswith(TAG_V1_MAGIC[:1]) else b""
            return iter(packets), tail
        view = view[idx:]
        magic = view[:2]
        tag_version = 1 if magic == TAG_V1_MAGIC else 2
        header_len = (
            TAG_V1_HEADER_LEN if magic == TAG_V1_MAGIC else TAG_V2_HEADER_LEN
        )
        if len(view) < header_len:
            return iter(packets), view
        stream_type = view[2]
        payload_len = struct.unpack_from("<H", view, 3)[0]
        expected_lengths = _payload_lengths(tag_version, stream_type)
        if expected_lengths is None or payload_len not in expected_lengths:
            view = view[2:]
            continue
        frame_len = header_len + payload_len
        if len(view) < frame_len:
            return iter(packets), view
        try:
            packets.append(parse_usb_packet(view[:frame_len]))
            view = view[frame_len:]
        except OgloProtocolError:
            view = view[2:]
