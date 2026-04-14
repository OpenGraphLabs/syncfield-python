"""Curated :class:`BLEImuProfile` presets for common BLE IMUs.

Each preset encodes one sensor family's wire protocol — frame layout,
channel scaling, and any one-time configuration commands — so calling
code can construct a working :class:`BLEImuGenericStream` with just an
address or a name filter::

    from syncfield.adapters import BLEImuGenericStream
    from syncfield.adapters.ble_imu_profiles import WIT_WT901BLE_200HZ

    stream = BLEImuGenericStream(
        id="wrist_left_imu",
        profile=WIT_WT901BLE_200HZ,
        ble_name="WT901BLE",
    )

Adding a new sensor
-------------------

A preset is a plain :class:`BLEImuProfile` literal. Three things to
fill in from the vendor datasheet:

1. The notify characteristic UUID and, if applicable, the write
   characteristic UUID for configuration.
2. The on-the-wire frame layout — magic-byte prefix (if any), then a
   :mod:`struct` format and one :class:`ChannelSpec` per decoded value
   with the correct linear scale to physical units.
3. The configuration sequence (unlock / mode select / rate set) the
   firmware demands before it will stream the expected format.

Submit new presets alongside a short comment block citing the vendor
spec section you copied the values from — the next reader will thank
you.
"""

from __future__ import annotations

from typing import Union

from syncfield.adapters.ble_imu import (
    BLEImuProfile,
    ChannelSpec,
    ConfigWrite,
)


# ============================================================================
# WitMotion WT901BLE / WT9011 family
# ============================================================================
#
# Spec references (WitMotion "BLE IMU Register Protocol", rev 2024-01):
#
#   Service       0xFFE5
#   Notify char   0xFFE4   — auto-output data frames
#   Write char    0xFFE9   — register commands (5 bytes each, little-endian)
#
# Default auto-output frame (reg 0x61 "combined short frame"), 20 bytes:
#
#   offset  size  meaning
#   ------  ----  -----------------------------------------------
#   0       2     magic prefix 0x55 0x61
#   2       6     accel x/y/z   — int16, scale = 16 / 32768   → g
#   8       6     gyro  x/y/z   — int16, scale = 2000 / 32768 → deg/s
#   14      6     angle r/p/y   — int16, scale = 180 / 32768  → deg
#
# Register commands all take the form  FF AA <addr> <low> <high>:
#
#   UNLOCK      addr=0x69, value=0xB588   → FF AA 69 88 B5
#   RATE        addr=0x03, value=<code>   → FF AA 03 <code> 00
#   SAVE        addr=0x00, value=0x0000   → FF AA 00 00 00
#
# Rate codes (register 0x03 low byte):
#
#   0x01=0.2  0x02=0.5  0x03=1   0x04=2   0x05=5   0x06=10 (factory default)
#   0x07=20   0x08=50   0x09=100 0x0A=125 0x0B=200
#
# macOS caveat: the WT901BLE68 firmware sends one sample per BLE
# notification and does not request a short connection interval.
# CoreBluetooth typically negotiates ≥15 ms intervals, capping actual
# notification throughput around 60–100 Hz regardless of the rate
# register. The sensor's internal sampling does hit 200 Hz — you just
# won't see every sample over the BLE link on Mac. For guaranteed
# 200 Hz use a Linux host (BlueZ permits 7.5 ms intervals) or wire the
# sensor over the USB-TTL adapter.


_WIT_SERVICE_UUID = "0000ffe5-0000-1000-8000-00805f9a34fb"
_WIT_NOTIFY_UUID = "0000ffe4-0000-1000-8000-00805f9a34fb"
_WIT_WRITE_UUID = "0000ffe9-0000-1000-8000-00805f9a34fb"

_WIT_FRAME_HEADER = b"\x55\x61"
_WIT_STRUCT_FORMAT = "<9h"   # accel(3) + gyro(3) + angle(3) as int16

_WIT_CHANNELS = (
    ChannelSpec("ax", scale=16.0 / 32768.0, unit="g"),
    ChannelSpec("ay", scale=16.0 / 32768.0, unit="g"),
    ChannelSpec("az", scale=16.0 / 32768.0, unit="g"),
    ChannelSpec("gx", scale=2000.0 / 32768.0, unit="deg/s"),
    ChannelSpec("gy", scale=2000.0 / 32768.0, unit="deg/s"),
    ChannelSpec("gz", scale=2000.0 / 32768.0, unit="deg/s"),
    ChannelSpec("roll",  scale=180.0 / 32768.0, unit="deg"),
    ChannelSpec("pitch", scale=180.0 / 32768.0, unit="deg"),
    ChannelSpec("yaw",   scale=180.0 / 32768.0, unit="deg"),
)

_WIT_RATE_CODES = {
    0.2: 0x01, 0.5: 0x02, 1: 0x03, 2: 0x04, 5: 0x05,
    10: 0x06, 20: 0x07, 50: 0x08, 100: 0x09, 125: 0x0A, 200: 0x0B,
}

# Inter-command delay recommended by WitMotion's own SDK reference —
# back-to-back writes without spacing occasionally drop the second
# command on cheaper WT-series modules.
_WIT_CMD_DELAY_S = 0.15


def wit_wt901ble(
    output_rate_hz: Union[int, float] = 200,
    *,
    save: bool = False,
) -> BLEImuProfile:
    """Build a :class:`BLEImuProfile` for WitMotion WT901BLE / WT9011.

    Args:
        output_rate_hz: Sensor-side sampling rate. One of 0.2, 0.5, 1,
            2, 5, 10, 20, 50, 100, 125, 200. Remember that actual BLE
            notification throughput is capped below this by the host's
            connection-interval policy (see module docstring).
        save: Persist the rate setting to the module's non-volatile
            memory so it survives power-cycles. Defaults to ``False``
            so repeated reconnects don't wear flash; set ``True`` when
            provisioning a freshly-unboxed device.

    Returns:
        A ready-to-use :class:`BLEImuProfile`.
    """
    if output_rate_hz not in _WIT_RATE_CODES:
        supported = ", ".join(str(k) for k in _WIT_RATE_CODES)
        raise ValueError(
            f"WT901BLE output_rate_hz={output_rate_hz} not supported. "
            f"Choose one of: {supported}"
        )
    rate_code = _WIT_RATE_CODES[output_rate_hz]

    config_writes = [
        # Unlock register access — mandatory before any RATE/SAVE write.
        ConfigWrite(
            char_uuid=_WIT_WRITE_UUID,
            data=b"\xff\xaa\x69\x88\xb5",
            delay_after_s=_WIT_CMD_DELAY_S,
        ),
        # Set output rate.
        ConfigWrite(
            char_uuid=_WIT_WRITE_UUID,
            data=bytes([0xff, 0xaa, 0x03, rate_code, 0x00]),
            delay_after_s=_WIT_CMD_DELAY_S,
        ),
    ]
    if save:
        config_writes.append(ConfigWrite(
            char_uuid=_WIT_WRITE_UUID,
            data=b"\xff\xaa\x00\x00\x00",
            delay_after_s=_WIT_CMD_DELAY_S,
        ))

    # Sensor internal sampling period. Used to interpolate per-sample
    # timestamps when the firmware bundles multiple (header + body)
    # sub-frames into one BLE notification at higher rates (e.g.
    # WT901BLE68 packs 8 samples per notification at 200 Hz).
    sample_period_us = int(round(1_000_000 / output_rate_hz))

    return BLEImuProfile(
        notify_uuid=_WIT_NOTIFY_UUID,
        struct_format=_WIT_STRUCT_FORMAT,
        channels=_WIT_CHANNELS,
        frame_header=_WIT_FRAME_HEADER,
        config_writes=tuple(config_writes),
        # samples_per_frame is left auto — the firmware's bundling
        # factor varies with the rate, and the adapter derives N from
        # each notification's length.
        sample_period_us=sample_period_us,
        description=(
            f"WitMotion WT901BLE / WT9011 @ {output_rate_hz} Hz "
            f"(accel g · gyro deg/s · angle deg)"
        ),
    )


#: Convenience preset: WitMotion WT901BLE at the maximum 200 Hz setting.
#: Equivalent to ``wit_wt901ble(200)``. Actual BLE throughput on macOS
#: is typically 60–100 Hz — see module docstring.
WIT_WT901BLE_200HZ: BLEImuProfile = wit_wt901ble(200)

#: Convenience preset: WitMotion WT901BLE at 100 Hz. A safer default
#: on macOS where the BLE link can't reliably sustain 200 Hz.
WIT_WT901BLE_100HZ: BLEImuProfile = wit_wt901ble(100)
