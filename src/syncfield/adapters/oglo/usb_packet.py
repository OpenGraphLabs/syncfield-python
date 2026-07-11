"""Pure parser for the OGLO USB CDC-ACM binary frame (``STREAM BIN ON``).

Separate from the BLE ``packed12_v5`` parser (``packet.py``) because the wire
format is genuinely different: the USB frame carries **16-bit** taxels (not
BLE's 12-bit packed pairs), a 2-byte magic, one sample per frame (not a batch),
and a 17-byte IMU block that additionally carries fused roll/pitch. Firmware
source of truth: ``oglo-hardware/firmware/OGLO-MT-RDR-02/oglo_rdr02_ble.ino``
(``writeSerialBinaryFrame`` / ``putImuBlock``).

Wire format (little-endian), one frame per sample, ~92 Hz over USB CDC::

    +0    u8[2]   magic         0xAA 0x55
    +2    u32     ts_us         device micros() (wraps ~71 min)
    +6    80 x u16 taxels       raw 12-bit ADC in a 16-bit field (0..4095),
                                finger,row,col order — same order as BLE
    +166  8 x i16 imu           roll_cdeg, pitch_cdeg, ax, ay, az, gx, gy, gz
    +182  u8      ok            1 = IMU sample valid
    = 183 bytes

The decoded sample reuses ``packet.OgloSample`` so everything downstream (the
stream adapter's ``_handle_payload``, drop detection, the writer) is transport-
agnostic: ``OgloSample.imu`` is the same 6-axis raw ``(ax..gz)`` tuple the BLE
path produces. The USB-only fused ``(roll_cdeg, pitch_cdeg)`` is exposed on the
richer ``UsbFrame`` for callers that want it, and dropped when narrowing to an
``OgloSample``.

No ``pyserial``/``asyncio`` dependency — the reader owns the socket; this
module only turns bytes into samples, so the wire format is unit-testable in
isolation, exactly like ``packet.py``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator, Tuple

from syncfield.adapters.oglo.packet import (
    DEFAULT_VALUES_PER_SAMPLE,
    OgloProtocolError,
    OgloSample,
)

__all__ = [
    "USB_MAGIC",
    "USB_FRAME_LEN",
    "UsbFrame",
    "parse_usb_frame",
    "iter_usb_frames",
    "STREAM_ON_COMMAND",
    "STREAM_OFF_COMMAND",
]

#: Frame delimiter — the firmware writes this as the first two bytes.
USB_MAGIC = b"\xAA\x55"

#: 2 (magic) + 4 (ts_us) + 80*2 (taxels) + 8*2 (imu) + 1 (ok).
_TAXEL_OFF = 6
_IMU_OFF = _TAXEL_OFF + DEFAULT_VALUES_PER_SAMPLE * 2  # 166
USB_FRAME_LEN = _IMU_OFF + 8 * 2 + 1  # 183

#: The firmware commands that gate the binary stream (see the .ino command
#: parser). Newline-terminated; send STREAM_ON after clearing any partial line.
STREAM_ON_COMMAND = b"STREAM BIN ON\n"
STREAM_OFF_COMMAND = b"STREAM BIN OFF\n"

_TAXEL_STRUCT = struct.Struct(f"<{DEFAULT_VALUES_PER_SAMPLE}H")
_IMU_STRUCT = struct.Struct("<8h")  # roll, pitch, ax, ay, az, gx, gy, gz


@dataclass(frozen=True)
class UsbFrame:
    """One decoded USB binary frame — richer than ``OgloSample`` (adds fused
    roll/pitch and the IMU-valid flag the USB firmware sends)."""

    device_us: int
    taxels: Tuple[int, ...]
    imu_raw: Tuple[int, int, int, int, int, int]  # ax, ay, az, gx, gy, gz
    roll_cdeg: int
    pitch_cdeg: int
    imu_ok: bool

    def to_sample(self, seq: int) -> OgloSample:
        """Narrow to the transport-agnostic ``OgloSample``. ``seq`` is supplied
        by the caller because the USB frame carries no global sequence number
        (unlike BLE's ``seq_base``); the reader counts frames instead."""
        return OgloSample(
            seq=seq,
            device_us=self.device_us,
            taxels=self.taxels,
            imu=self.imu_raw if self.imu_ok else None,
        )


def parse_usb_frame(frame: bytes) -> UsbFrame:
    """Parse one 183-byte frame (magic included). Raises ``OgloProtocolError``
    on a wrong length or bad magic — the reader resyncs on the next magic."""
    if len(frame) != USB_FRAME_LEN:
        raise OgloProtocolError(f"USB frame must be {USB_FRAME_LEN} bytes, got {len(frame)}")
    if frame[:2] != USB_MAGIC:
        raise OgloProtocolError(f"bad USB frame magic: {frame[:2].hex()}")

    (device_us,) = struct.unpack_from("<I", frame, 2)
    taxels = _TAXEL_STRUCT.unpack_from(frame, _TAXEL_OFF)
    roll, pitch, ax, ay, az, gx, gy, gz = _IMU_STRUCT.unpack_from(frame, _IMU_OFF)
    ok = frame[USB_FRAME_LEN - 1] != 0

    return UsbFrame(
        device_us=device_us,
        taxels=taxels,
        imu_raw=(ax, ay, az, gx, gy, gz),
        roll_cdeg=roll,
        pitch_cdeg=pitch,
        imu_ok=ok,
    )


def iter_usb_frames(buffer: bytes) -> Tuple[Iterator[UsbFrame], bytes]:
    """Extract every complete frame from a rolling byte buffer.

    Resynchronises on the ``0xAA 0x55`` magic, so a partial write, a leftover
    ``#HB`` text line, or a dropped byte costs at most one frame rather than
    desyncing the stream. Returns ``(frames, leftover)`` where *leftover* is the
    tail to prepend to the next read. A frame whose magic is present but whose
    body is malformed is skipped (its magic consumed) so a single bad frame
    cannot wedge the reader.
    """
    frames = []
    view = buffer
    while True:
        idx = view.find(USB_MAGIC)
        if idx < 0:
            # No magic at all — keep only a trailing partial magic byte.
            tail = view[-1:] if view[-1:] == USB_MAGIC[:1] else b""
            return iter(frames), tail
        if len(view) - idx < USB_FRAME_LEN:
            # Magic found but frame not fully arrived yet — keep from the magic.
            return iter(frames), view[idx:]
        candidate = view[idx : idx + USB_FRAME_LEN]
        try:
            frames.append(parse_usb_frame(candidate))
            view = view[idx + USB_FRAME_LEN :]
        except OgloProtocolError:
            # Bad body — drop just this magic and rescan from the next byte.
            view = view[idx + len(USB_MAGIC) :]

