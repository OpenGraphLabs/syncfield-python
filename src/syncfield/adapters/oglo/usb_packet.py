"""OGLO firmware 0.9.3 schema-6 USB tagged-stream parser.

Each modality is independently framed and timestamped::

    A5 5A | type:u8 | payload_len:u16le | seq:u32le | t_us:u32le | payload

The parser deliberately accepts only the production v6 payload sizes.  A bad
header is discarded up to the next magic; a partial frame remains buffered.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator, Tuple

from syncfield.adapters.oglo.packet import OgloProtocolError

__all__ = [
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
    "QUIET_COMMANDS",
]

TAG_MAGIC = b"\xA5\x5A"
TAG_HEADER_LEN = 13
TAG_TYPE_TACTILE = 1
TAG_TYPE_IMU = 2
TAG_TYPE_MAG = 3

_PAYLOAD_LENGTHS = {TAG_TYPE_TACTILE: 160, TAG_TYPE_IMU: 12, TAG_TYPE_MAG: 6}
_STRUCTS = {
    TAG_TYPE_TACTILE: struct.Struct("<80H"),
    TAG_TYPE_IMU: struct.Struct("<6h"),
    TAG_TYPE_MAG: struct.Struct("<3h"),
}

STREAM_ON_COMMAND = b"STREAM TAG ON\n"
STREAM_OFF_COMMAND = b"STREAM TAG OFF\n"
QUIET_COMMANDS = b"\nSTREAM TAG OFF\nSTREAM BIN OFF\nSTREAM TAXEL OFF\n"


@dataclass(frozen=True)
class UsbTaggedPacket:
    stream_type: int
    seq: int
    device_us: int
    values: Tuple[int, ...]

    @property
    def modality(self) -> str:
        return {TAG_TYPE_TACTILE: "tactile", TAG_TYPE_IMU: "imu", TAG_TYPE_MAG: "mag"}[self.stream_type]

    @property
    def device_ns(self) -> int:
        return self.device_us * 1_000


def parse_usb_packet(frame: bytes) -> UsbTaggedPacket:
    if len(frame) < TAG_HEADER_LEN:
        raise OgloProtocolError(f"USB tag frame shorter than {TAG_HEADER_LEN} bytes")
    if frame[:2] != TAG_MAGIC:
        raise OgloProtocolError(f"bad USB tag magic: {frame[:2].hex()}")
    stream_type = frame[2]
    payload_len = struct.unpack_from("<H", frame, 3)[0]
    expected_len = _PAYLOAD_LENGTHS.get(stream_type)
    if expected_len is None:
        raise OgloProtocolError(f"unknown USB tag stream type {stream_type}")
    if payload_len != expected_len:
        raise OgloProtocolError(
            f"USB tag {stream_type} payload must be {expected_len} bytes, got {payload_len}"
        )
    if len(frame) != TAG_HEADER_LEN + payload_len:
        raise OgloProtocolError(
            f"USB tag frame must be {TAG_HEADER_LEN + payload_len} bytes, got {len(frame)}"
        )
    seq, device_us = struct.unpack_from("<II", frame, 5)
    values = _STRUCTS[stream_type].unpack_from(frame, TAG_HEADER_LEN)
    return UsbTaggedPacket(stream_type, seq, device_us, values)


def iter_usb_packets(buffer: bytes) -> tuple[Iterator[UsbTaggedPacket], bytes]:
    packets: list[UsbTaggedPacket] = []
    view = buffer
    while True:
        idx = view.find(TAG_MAGIC)
        if idx < 0:
            tail = view[-1:] if view.endswith(TAG_MAGIC[:1]) else b""
            return iter(packets), tail
        view = view[idx:]
        if len(view) < TAG_HEADER_LEN:
            return iter(packets), view
        stream_type = view[2]
        payload_len = struct.unpack_from("<H", view, 3)[0]
        expected_len = _PAYLOAD_LENGTHS.get(stream_type)
        if expected_len is None or payload_len != expected_len:
            view = view[2:]
            continue
        frame_len = TAG_HEADER_LEN + payload_len
        if len(view) < frame_len:
            return iter(packets), view
        try:
            packets.append(parse_usb_packet(view[:frame_len]))
            view = view[frame_len:]
        except OgloProtocolError:
            view = view[2:]
