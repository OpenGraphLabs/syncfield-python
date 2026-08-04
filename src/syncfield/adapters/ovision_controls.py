"""Fail-closed production image profile for OVISION cameras.

HAMPO exposes the controls that materially affect the encoded image through
its vendor UVC Extension Unit rather than standard V4L2 controls.  Configure
and verify them before opening the video stream so every capture starts from a
known profile, including after a camera power cycle.
"""

from __future__ import annotations

import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from syncfield.adapters.ovision_calibration import _xu_query


SELECTOR_EXPOSURE_TIME = 0x07
SELECTOR_SYSTEM_GAIN = 0x08
SELECTOR_BITRATE = 0x09

UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
UVC_GET_MIN = 0x82
UVC_GET_MAX = 0x83
UVC_GET_RES = 0x84
UVC_GET_LEN = 0x85
UVC_GET_INFO = 0x86

# Device-reported limits on the production batch.  The longest exposure and
# minimum analog/system gain maximize sensor SNR without exceeding a 10 ms
# shutter, while the maximum CVBR target minimizes H.264 loss on noisy global-
# shutter frames.  All three limits are still discovered and validated at
# runtime so incompatible firmware fails closed.
PRODUCTION_EXPOSURE_TIME_US = 10_000
PRODUCTION_SYSTEM_GAIN = 1_024  # 22.10 fixed point: 1.0x
PRODUCTION_BITRATE_KBPS = 15_360


class OvisionControlError(RuntimeError):
    """The OVISION image profile could not be applied exactly."""


@dataclass(frozen=True)
class OvisionCaptureProfile:
    exposure_time_us: int
    system_gain_raw: int
    bitrate_kbps: int

    @property
    def gain_multiplier(self) -> float:
        return self.system_gain_raw / 1024.0

    def capture_document(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "system_gain_multiplier": self.gain_multiplier,
            "source": "ovision_uvc_extension_unit_readback",
        }


@dataclass(frozen=True)
class _ControlCapability:
    minimum: int
    maximum: int
    resolution: int


def _read_u32(video_device: str | Path, selector: int, query: int) -> int:
    return struct.unpack("<I", _xu_query(video_device, selector, query, 4))[0]


def _probe_control(video_device: str | Path, selector: int) -> _ControlCapability:
    length = struct.unpack(
        "<H", _xu_query(video_device, selector, UVC_GET_LEN, 2)
    )[0]
    info = _xu_query(video_device, selector, UVC_GET_INFO, 1)[0]
    if length != 4:
        raise OvisionControlError(
            f"OVISION selector 0x{selector:02x} has length {length}, expected 4"
        )
    if info & 0x03 != 0x03:
        raise OvisionControlError(
            f"OVISION selector 0x{selector:02x} must support GET and SET, info=0x{info:02x}"
        )
    capability = _ControlCapability(
        minimum=_read_u32(video_device, selector, UVC_GET_MIN),
        maximum=_read_u32(video_device, selector, UVC_GET_MAX),
        resolution=_read_u32(video_device, selector, UVC_GET_RES),
    )
    if capability.resolution <= 0 or capability.minimum > capability.maximum:
        raise OvisionControlError(
            f"OVISION selector 0x{selector:02x} reported invalid limits: {capability}"
        )
    return capability


def _validate_requested(
    selector: int, requested: int, capability: _ControlCapability
) -> None:
    if not capability.minimum <= requested <= capability.maximum:
        raise OvisionControlError(
            f"OVISION selector 0x{selector:02x} requested {requested} outside "
            f"[{capability.minimum}, {capability.maximum}]"
        )
    if (requested - capability.minimum) % capability.resolution:
        raise OvisionControlError(
            f"OVISION selector 0x{selector:02x} requested {requested} does not align "
            f"to resolution {capability.resolution} from {capability.minimum}"
        )


def _write_and_verify(video_device: str | Path, selector: int, requested: int) -> int:
    _xu_query(
        video_device,
        selector,
        UVC_SET_CUR,
        4,
        struct.pack("<I", requested),
    )
    actual = _read_u32(video_device, selector, UVC_GET_CUR)
    if actual != requested:
        raise OvisionControlError(
            f"OVISION selector 0x{selector:02x} readback {actual}, requested {requested}"
        )
    return actual


def configure_ovision_capture_profile(
    video_device: str | Path,
    *,
    exposure_time_us: int = PRODUCTION_EXPOSURE_TIME_US,
    system_gain_raw: int = PRODUCTION_SYSTEM_GAIN,
    bitrate_kbps: int = PRODUCTION_BITRATE_KBPS,
) -> OvisionCaptureProfile:
    """Apply and verify the production OVISION image profile.

    All controls are probed before the first write.  A camera with incompatible
    firmware therefore fails before capture instead of silently recording with
    an unknown exposure, gain, or encoder bitrate.
    """

    requested = (
        (SELECTOR_EXPOSURE_TIME, int(exposure_time_us)),
        (SELECTOR_SYSTEM_GAIN, int(system_gain_raw)),
        (SELECTOR_BITRATE, int(bitrate_kbps)),
    )
    for selector, value in requested:
        _validate_requested(selector, value, _probe_control(video_device, selector))

    actual = {
        selector: _write_and_verify(video_device, selector, value)
        for selector, value in requested
    }
    return OvisionCaptureProfile(
        exposure_time_us=actual[SELECTOR_EXPOSURE_TIME],
        system_gain_raw=actual[SELECTOR_SYSTEM_GAIN],
        bitrate_kbps=actual[SELECTOR_BITRATE],
    )
