"""Pure parser for the OGLO ``packed12_v5`` BLE notify packet.

This module has **no** ``bleak`` / ``asyncio`` dependency so the wire format
can be unit-tested in isolation. The layout is the firmware source of truth
(``oglo-hardware/firmware/OGLO-MT-RDR-02/oglo_rdr02_ble.ino`` +
``BLE_PACKET_FORMAT_v5.md``), cross-checked against the ``syncfield-swift``
``TactilePacketParser``.

Wire format (``FW_REV 0.7.1-cfgfit``, ``schema_ver 5``, little-endian)::

    Header — 10 bytes
      +0  u8    sample_count  (N, normally 3)
      +1  u8    flags         (bit 0x04 = packed12 + per-sample IMU)
      +2  u32   seq_base      (global sample seq # of sample 0)
      +6  u32   t_base_us     (device micros of sample 0)

    Per-sample slot — 134 bytes, repeated N times
      +0    u16       dt_us       (t_us[i] - t_base_us; sample 0 == 0)
      +2    120 bytes taxels      (80 x 12-bit, 2 taxels per 3 bytes)
      +122  6 x i16   imu_raw     (ax, ay, az, gx, gy, gz; raw LSB)

The wire *format* is detected from the ``flags`` byte, never from the config
manifest — the shipped firmware keeps its config JSON lean and does not carry
``packet_format``/``sample_stride`` keys. Only ``packed12`` (flags ``0x04``)
is supported; legacy Method C/B raise :class:`OgloProtocolError`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional, Tuple

__all__ = [
    "OgloProtocolError",
    "OgloSample",
    "OgloPacket",
    "unpack_taxels12",
    "parse_v5",
    "HEADER_LEN",
    "SAMPLE_STRIDE",
    "TAXEL_PACKED_LEN",
    "IMU_RAW_LEN",
    "DEFAULT_VALUES_PER_SAMPLE",
    "ADC_MAX_VALUE",
    "FLAG_PACKED",
]

#: Notify header length in bytes.
HEADER_LEN = 10
#: Packed-taxel block length for 80 taxels (``(80 * 12 + 7) // 8``).
TAXEL_PACKED_LEN = 120
#: Raw-IMU block length: 6 x int16.
IMU_RAW_LEN = 12
#: Full per-sample slot stride: ``dt_us(2) + taxels(120) + imu(12)``.
SAMPLE_STRIDE = 2 + TAXEL_PACKED_LEN + IMU_RAW_LEN  # 134
#: Default taxel count (5 fingers x 4 rows x 4 cols); overridable by manifest.
DEFAULT_VALUES_PER_SAMPLE = 80
#: 12-bit ADC full scale.
ADC_MAX_VALUE = 4095
#: ``flags`` byte bit selecting the packed12 + per-sample-IMU format.
FLAG_PACKED = 0x04


class OgloProtocolError(Exception):
    """Raised when a packet or manifest does not match the packed12 v5 protocol.

    Deliberately fail-loud: the adapter only supports the current firmware's
    default format and never silently falls back to a legacy parse.
    """


@dataclass(frozen=True)
class OgloSample:
    """One decoded tactile sample within a packet.

    Attributes:
        seq: Global sample sequence number (``seq_base + i``); use gaps
            across packets to detect dropped notifications.
        device_us: Device-clock timestamp in microseconds
            (``t_base_us + dt_us``). Raw ESP32 ``micros()`` — wraps ~71 min.
        taxels: ``values_per_sample`` raw 12-bit ADC counts (0..4095), in
            ``finger, row, col`` order.
        imu: Raw IMU ``(ax, ay, az, gx, gy, gz)`` as signed int16 LSB counts,
            or ``None`` when a truncated trailing IMU block was dropped
            (taxels are always fully present).
    """

    seq: int
    device_us: int
    taxels: Tuple[int, ...]
    imu: Optional[Tuple[int, int, int, int, int, int]]

    @property
    def device_ns(self) -> int:
        """Device timestamp in nanoseconds (``device_us * 1000``)."""
        return self.device_us * 1000


@dataclass(frozen=True)
class OgloPacket:
    """A decoded packed12 v5 notify packet."""

    seq_base: int
    t_base_us: int
    samples: Tuple[OgloSample, ...]

    @property
    def count(self) -> int:
        return len(self.samples)


def unpack_taxels12(buf: bytes, count: int) -> Tuple[int, ...]:
    """Unpack ``count`` 12-bit taxels from a packed byte buffer.

    Taxels are packed two-per-three-bytes. For a triplet ``(b0, b1, b2)``::

        v_even = (b0 << 4) | (b1 >> 4)        # taxel 2k
        v_odd  = ((b1 & 0x0F) << 8) | b2      # taxel 2k+1

    Args:
        buf: Packed taxel bytes (``>= (count * 3 + 1) // 2`` long).
        count: Number of taxel values to produce.

    Returns:
        Tuple of ``count`` ints in ``0..4095``.
    """
    out = []
    triplets = (count + 1) // 2
    for k in range(triplets):
        b0 = buf[3 * k]
        b1 = buf[3 * k + 1]
        b2 = buf[3 * k + 2]
        out.append((b0 << 4) | (b1 >> 4))
        if len(out) < count:
            out.append(((b1 & 0x0F) << 8) | b2)
    return tuple(out)


def parse_v5(
    payload: bytes,
    *,
    values_per_sample: int = DEFAULT_VALUES_PER_SAMPLE,
) -> OgloPacket:
    """Parse one packed12 v5 notify packet.

    Args:
        payload: Raw notification bytes.
        values_per_sample: Taxel count per sample (from the manifest;
            defaults to 80). Drives the packed-taxel block length.

    Returns:
        The decoded :class:`OgloPacket`.

    Raises:
        OgloProtocolError: The payload is shorter than the header, the
            ``flags`` byte does not select packed12 (``0x04``), or a slot's
            taxel block is truncated. A truncated *trailing IMU* block does
            not raise — that sample's ``imu`` is ``None`` and its taxels are
            preserved.
    """
    if len(payload) < HEADER_LEN:
        raise OgloProtocolError(f"short packet: {len(payload)} bytes < {HEADER_LEN}")

    count = payload[0]
    flags = payload[1]
    if not (flags & FLAG_PACKED):
        raise OgloProtocolError(
            f"unsupported framing: flags=0x{flags:02x} "
            f"(expected packed12 bit 0x{FLAG_PACKED:02x}); "
            "legacy Method C/B is not supported"
        )

    seq_base, t_base_us = struct.unpack_from("<II", payload, 2)
    packed_len = (values_per_sample * 3 + 1) // 2

    samples = []
    for i in range(count):
        slot = HEADER_LEN + i * SAMPLE_STRIDE
        taxel_end = slot + 2 + packed_len
        if taxel_end > len(payload):
            raise OgloProtocolError(
                f"truncated: sample {i} needs {taxel_end} bytes, "
                f"payload has {len(payload)}"
            )
        (dt_us,) = struct.unpack_from("<H", payload, slot)
        taxels = unpack_taxels12(payload[slot + 2 : taxel_end], values_per_sample)

        imu: Optional[Tuple[int, int, int, int, int, int]]
        if taxel_end + IMU_RAW_LEN <= len(payload):
            imu = struct.unpack_from("<6h", payload, taxel_end)  # type: ignore[assignment]
        else:
            # Best-effort IMU: a truncated trailing block drops IMU for this
            # sample but keeps its taxels (matches the Swift reference).
            imu = None

        samples.append(
            OgloSample(
                seq=seq_base + i,
                device_us=t_base_us + dt_us,
                taxels=taxels,
                imu=imu,
            )
        )

    return OgloPacket(seq_base=seq_base, t_base_us=t_base_us, samples=tuple(samples))
