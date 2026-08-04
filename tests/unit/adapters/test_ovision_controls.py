from __future__ import annotations

import struct

import pytest

from syncfield.adapters import ovision_controls
from syncfield.adapters.ovision_controls import (
    OvisionControlError,
    configure_ovision_capture_profile,
)


def _fake_xu(monkeypatch: pytest.MonkeyPatch, *, info: int = 3, mismatch: bool = False):
    values = {0x07: 9_000, 0x08: 2_048, 0x09: 12_288}
    limits = {
        0x07: (2_000, 10_000, 1),
        0x08: (1_024, 0xFFFFFFFF, 1),
        0x09: (8_192, 15_360, 1),
    }
    writes: list[tuple[int, int]] = []

    def query(_path, selector, request, size, payload=None):
        assert size in (1, 2, 4)
        if request == ovision_controls.UVC_GET_LEN:
            return struct.pack("<H", 4)
        if request == ovision_controls.UVC_GET_INFO:
            return bytes([info])
        if request == ovision_controls.UVC_GET_MIN:
            return struct.pack("<I", limits[selector][0])
        if request == ovision_controls.UVC_GET_MAX:
            return struct.pack("<I", limits[selector][1])
        if request == ovision_controls.UVC_GET_RES:
            return struct.pack("<I", limits[selector][2])
        if request == ovision_controls.UVC_SET_CUR:
            value = struct.unpack("<I", payload)[0]
            writes.append((selector, value))
            values[selector] = value
            return payload
        if request == ovision_controls.UVC_GET_CUR:
            value = values[selector]
            if mismatch and selector == ovision_controls.SELECTOR_BITRATE:
                value -= 1
            return struct.pack("<I", value)
        raise AssertionError(request)

    monkeypatch.setattr(ovision_controls, "_xu_query", query)
    return writes


def test_production_profile_is_probed_set_and_read_back(monkeypatch: pytest.MonkeyPatch) -> None:
    writes = _fake_xu(monkeypatch)

    profile = configure_ovision_capture_profile("/dev/video-test")

    assert writes == [(0x07, 10_000), (0x08, 1_024), (0x09, 15_360)]
    assert profile.exposure_time_us == 10_000
    assert profile.gain_multiplier == 1.0
    assert profile.bitrate_kbps == 15_360
    assert profile.capture_document()["source"] == "ovision_uvc_extension_unit_readback"


def test_incompatible_firmware_fails_before_any_write(monkeypatch: pytest.MonkeyPatch) -> None:
    writes = _fake_xu(monkeypatch, info=1)

    with pytest.raises(OvisionControlError, match="must support GET and SET"):
        configure_ovision_capture_profile("/dev/video-test")

    assert writes == []


def test_readback_mismatch_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_xu(monkeypatch, mismatch=True)

    with pytest.raises(OvisionControlError, match="readback 15359, requested 15360"):
        configure_ovision_capture_profile("/dev/video-test")


def test_requested_value_must_fit_device_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    writes = _fake_xu(monkeypatch)

    with pytest.raises(OvisionControlError, match="outside"):
        configure_ovision_capture_profile(
            "/dev/video-test", exposure_time_us=10_001
        )

    assert writes == []
