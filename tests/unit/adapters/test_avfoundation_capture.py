"""Unit tests for the pure (non-ObjC) parts of the native AVFoundation backend."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

import pytest

from syncfield.adapters._avfoundation_capture import (
    AVFoundationUnavailable,
    _resolve_device,
    bgr_from_pixel_buffer,
    select_capture_format,
)


# --- _resolve_device (identity by unique_id) -------------------------------


class _FakeAVF:
    def __init__(self, by_uid: dict) -> None:
        self._by_uid = by_uid

        outer = self

        class AVCaptureDevice:  # noqa: N801 - mirrors the ObjC class name
            @staticmethod
            def deviceWithUniqueID_(uid):  # noqa: N802
                return outer._by_uid.get(uid)

        self.AVCaptureDevice = AVCaptureDevice


def test_resolve_device_opens_by_unique_id() -> None:
    avf = _FakeAVF({"UID_A": "camA", "UID_B": "camB"})
    # Two identical cameras are told apart purely by unique_id.
    assert _resolve_device(avf, "UID_A", index=0) == "camA"
    assert _resolve_device(avf, "UID_B", index=0) == "camB"


def test_resolve_device_raises_when_unique_id_absent() -> None:
    avf = _FakeAVF({"UID_A": "camA"})
    with pytest.raises(AVFoundationUnavailable):
        _resolve_device(avf, "UID_GONE", index=0)


# --- select_capture_format ------------------------------------------------


class _Range:
    def __init__(self, max_fps: float, duration: str) -> None:
        self._max = max_fps
        self._dur = duration

    def maxFrameRate(self) -> float:  # noqa: N802 - ObjC selector name
        return self._max

    def minFrameDuration(self):  # noqa: N802 - ObjC selector name
        return self._dur


class _Format:
    def __init__(self, w: int, h: int, ranges: list[_Range]) -> None:
        self._desc = (w, h)
        self._ranges = ranges

    def formatDescription(self):  # noqa: N802 - ObjC selector name
        return self._desc

    def videoSupportedFrameRateRanges(self):  # noqa: N802 - ObjC selector name
        return self._ranges


class _Device:
    def __init__(self, formats: list[_Format]) -> None:
        self._formats = formats

    def formats(self):
        return self._formats


class _CM:
    @staticmethod
    def CMVideoFormatDescriptionGetDimensions(desc):  # noqa: N802
        return SimpleNamespace(width=desc[0], height=desc[1])


def test_select_capture_format_prefers_exact_dims_and_highest_fps() -> None:
    device = _Device(
        [
            _Format(1280, 720, [_Range(30, "d30_uncompressed")]),  # uncompressed
            _Format(
                1280, 720, [_Range(120, "d120"), _Range(30, "d30_mjpeg")]
            ),  # MJPEG-backed (120 fps impossible uncompressed over USB2)
            _Format(1920, 1080, [_Range(30, "d30_1080")]),
        ]
    )

    fmt, max_fps, cap_duration = select_capture_format(
        device, _CM, width=1280, height=720
    )

    assert max_fps == 120  # chose the MJPEG-backed high-fps 720p format
    assert cap_duration == "d30_mjpeg"  # 30 fps cap taken from that format


def test_select_capture_format_falls_back_to_closest_when_no_exact_match() -> None:
    device = _Device([_Format(640, 480, [_Range(30, "d30")])])
    fmt, max_fps, cap_duration = select_capture_format(
        device, _CM, width=1280, height=720
    )
    assert fmt is not None
    assert max_fps == 30


def test_select_capture_format_empty_device_returns_none() -> None:
    fmt, max_fps, cap_duration = select_capture_format(
        _Device([]), _CM, width=1280, height=720
    )
    assert fmt is None
    assert max_fps == 0.0
    assert cap_duration is None


# --- bgr_from_pixel_buffer -------------------------------------------------


class _Base:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def as_buffer(self, n: int) -> bytes:
        return self._data[:n]


class _Quartz:
    def __init__(self, w: int, h: int, bpr: int, data: bytes) -> None:
        self._w, self._h, self._bpr, self._data = w, h, bpr, data

    def CVPixelBufferGetWidth(self, _p):  # noqa: N802
        return self._w

    def CVPixelBufferGetHeight(self, _p):  # noqa: N802
        return self._h

    def CVPixelBufferGetBytesPerRow(self, _p):  # noqa: N802
        return self._bpr

    def CVPixelBufferGetBaseAddress(self, _p):  # noqa: N802
        return _Base(self._data)


def test_bgr_from_pixel_buffer_drops_alpha_and_row_padding() -> None:
    # 2x2 image, BGRA, with a padded stride of 12 bytes (3 pixels) per row.
    w, h, bpr = 2, 2, 12
    data = bytes(range(bpr * h))  # 0..23
    quartz = _Quartz(w, h, bpr, data)

    arr = bgr_from_pixel_buffer(quartz, object())

    assert arr.shape == (h, w, 3)
    assert arr.dtype == np.uint8
    # First pixel BGR = first three bytes; alpha (index 3) dropped.
    assert list(arr[0, 0]) == [0, 1, 2]
    # Second row starts at byte 12 (stride), not 8 — proves padding is respected.
    assert list(arr[1, 0]) == [12, 13, 14]
