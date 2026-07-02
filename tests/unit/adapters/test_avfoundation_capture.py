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
    """Discrete UVC rate range by default (min == max), continuous if told."""

    def __init__(self, max_fps: float, duration: str, min_fps: float | None = None) -> None:
        self._max = max_fps
        self._min = max_fps if min_fps is None else min_fps
        self._dur = duration

    def maxFrameRate(self) -> float:  # noqa: N802 - ObjC selector name
        return self._max

    def minFrameRate(self) -> float:  # noqa: N802 - ObjC selector name
        return self._min

    def minFrameDuration(self):  # noqa: N802 - ObjC selector name
        return f"{self._dur}_min"

    def maxFrameDuration(self):  # noqa: N802 - ObjC selector name
        return f"{self._dur}_max"


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

    @staticmethod
    def CMTimeMake(value, timescale):  # noqa: N802
        return ("cmtime", value, timescale)


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

    fmt, max_fps, lock_min, lock_max, pace_to = select_capture_format(
        device, _CM, width=1280, height=720, fps=30.0
    )

    assert max_fps == 120  # chose the MJPEG-backed high-fps 720p format
    # The discrete 30fps mode is locked from BOTH sides so neither a bright
    # scene (rate up) nor auto-exposure (rate down) can move the cadence.
    assert lock_min == "d30_mjpeg_min"
    assert lock_max == "d30_mjpeg_min"
    assert pace_to is None


def test_select_capture_format_locks_discrete_target_rate() -> None:
    """Real-world case: 4K webcam with discrete [30][25][20][15][10][5]."""
    device = _Device(
        [
            _Format(
                1280,
                720,
                [
                    _Range(30.00003, "d30"),
                    _Range(25, "d25"),
                    _Range(20, "d20"),
                ],
            )
        ]
    )
    fmt, _max_fps, lock_min, lock_max, pace_to = select_capture_format(
        device, _CM, width=1280, height=720, fps=30.0
    )
    assert lock_min == "d30_min"
    assert lock_max == "d30_min"
    assert pace_to is None


def test_select_capture_format_paces_when_camera_only_runs_faster() -> None:
    """Real-world case: 60-only camera (HC CAM) asked for 30 fps — lock 60 and
    decimate in software to an exact 2:1."""
    device = _Device([_Format(1280, 720, [_Range(60.00024, "d60")])])
    fmt, max_fps, lock_min, lock_max, pace_to = select_capture_format(
        device, _CM, width=1280, height=720, fps=30.0
    )
    assert lock_min == "d60_max"  # slowest rate of the range = longest duration
    assert lock_max == "d60_max"
    assert pace_to == 30.0


def test_select_capture_format_locks_fastest_when_target_unreachable() -> None:
    device = _Device([_Format(1280, 720, [_Range(20, "d20"), _Range(10, "d10")])])
    fmt, _max_fps, lock_min, lock_max, pace_to = select_capture_format(
        device, _CM, width=1280, height=720, fps=30.0
    )
    assert lock_min == "d20_min"
    assert lock_max == "d20_min"
    assert pace_to is None


def test_select_capture_format_builds_cmtime_for_continuous_range() -> None:
    device = _Device([_Format(1280, 720, [_Range(60, "cont", min_fps=1)])])
    fmt, _max_fps, lock_min, lock_max, pace_to = select_capture_format(
        device, _CM, width=1280, height=720, fps=30.0
    )
    assert lock_min == ("cmtime", 1_000_000, 30_000_000)
    assert lock_max == lock_min
    assert pace_to is None


def test_select_capture_format_falls_back_to_closest_when_no_exact_match() -> None:
    device = _Device([_Format(640, 480, [_Range(30, "d30")])])
    fmt, max_fps, lock_min, lock_max, pace_to = select_capture_format(
        device, _CM, width=1280, height=720, fps=30.0
    )
    assert fmt is not None
    assert max_fps == 30


def test_select_capture_format_empty_device_returns_none() -> None:
    fmt, max_fps, lock_min, lock_max, pace_to = select_capture_format(
        _Device([]), _CM, width=1280, height=720, fps=30.0
    )
    assert fmt is None
    assert max_fps == 0.0
    assert lock_min is None and lock_max is None and pace_to is None


def test_pacing_decimates_60fps_input_to_uniform_30() -> None:
    from syncfield.adapters._avfoundation_capture import NativeAVCapture

    cap = NativeAVCapture(device_index=0, width=1280, height=720, fps=30.0)
    cap._pace_period_ns = int(1e9 / 30)
    period_60 = int(1e9 / 60)
    accepted = [
        ts
        for i in range(120)
        if cap._accept_paced(ts := 1_000_000 + i * period_60)
    ]
    assert len(accepted) == 60  # exactly half of 120 input frames
    gaps = {round((b - a) / 1e6) for a, b in zip(accepted, accepted[1:])}
    assert gaps == {33}  # uniform ~33ms spacing


def test_pacing_passes_through_input_at_or_below_target() -> None:
    from syncfield.adapters._avfoundation_capture import NativeAVCapture

    cap = NativeAVCapture(device_index=0, width=1280, height=720, fps=30.0)
    cap._pace_period_ns = int(1e9 / 30)
    period_25 = int(1e9 / 25)
    accepted = sum(
        1 for i in range(100) if cap._accept_paced(1_000_000 + i * period_25)
    )
    assert accepted >= 95  # 25fps input flows through essentially untouched


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


# --- duplicate-open registry ------------------------------------------------


class _RegDevice:
    def __init__(self, uid: str, name: str = "cam") -> None:
        self._uid = uid
        self._name = name

    def uniqueID(self):  # noqa: N802 - ObjC selector name
        return self._uid

    def localizedName(self):  # noqa: N802 - ObjC selector name
        return self._name


@pytest.fixture()
def _clean_registry():
    from syncfield.adapters import _avfoundation_capture as mod

    with mod._OPEN_UNIQUE_IDS_LOCK:
        mod._OPEN_UNIQUE_IDS.clear()
    yield
    with mod._OPEN_UNIQUE_IDS_LOCK:
        mod._OPEN_UNIQUE_IDS.clear()


def test_second_open_of_same_camera_is_rejected(_clean_registry) -> None:
    from syncfield.adapters._avfoundation_capture import NativeAVCapture

    first = NativeAVCapture(device_index=0, width=1280, height=720, fps=30.0)
    second = NativeAVCapture(device_index=1, width=1280, height=720, fps=30.0)
    first._register_open_device(_RegDevice("UID_A"))
    with pytest.raises(AVFoundationUnavailable, match="already opened"):
        second._register_open_device(_RegDevice("UID_A"))


def test_registry_releases_camera_on_unregister(_clean_registry) -> None:
    from syncfield.adapters._avfoundation_capture import NativeAVCapture

    first = NativeAVCapture(device_index=0, width=1280, height=720, fps=30.0)
    first._register_open_device(_RegDevice("UID_A"))
    first._unregister_open_device()

    second = NativeAVCapture(device_index=1, width=1280, height=720, fps=30.0)
    second._register_open_device(_RegDevice("UID_A"))  # must not raise
    assert second._registered_uid == "UID_A"


def test_distinct_cameras_register_concurrently(_clean_registry) -> None:
    from syncfield.adapters._avfoundation_capture import NativeAVCapture

    a = NativeAVCapture(device_index=0, width=1280, height=720, fps=30.0)
    b = NativeAVCapture(device_index=1, width=1280, height=720, fps=30.0)
    a._register_open_device(_RegDevice("UID_A"))
    b._register_open_device(_RegDevice("UID_B"))
    assert a._registered_uid == "UID_A"
    assert b._registered_uid == "UID_B"


def test_stop_unregisters_even_without_session(_clean_registry) -> None:
    from syncfield.adapters._avfoundation_capture import NativeAVCapture

    cap = NativeAVCapture(device_index=0, width=1280, height=720, fps=30.0)
    cap._register_open_device(_RegDevice("UID_A"))
    cap.stop()
    fresh = NativeAVCapture(device_index=0, width=1280, height=720, fps=30.0)
    fresh._register_open_device(_RegDevice("UID_A"))  # released by stop()


# --- first-frame verification ------------------------------------------------


def test_start_raises_and_releases_registry_when_no_frames_arrive(
    monkeypatch, _clean_registry
) -> None:
    from syncfield.adapters import _avfoundation_capture as mod

    cap = mod.NativeAVCapture(
        device_index=0, width=640, height=360, fps=30.0, unique_id="UID_X"
    )
    cap._first_frame_timeout_s = 0.05
    cap._open_attempts = 2
    monkeypatch.setattr(mod, "_load_frameworks", lambda: (None,) * 6)
    monkeypatch.setattr(
        mod, "_resolve_device", lambda _avf, _uid, _idx: _RegDevice("UID_X")
    )
    starts: list[int] = []
    monkeypatch.setattr(
        cap,
        "_start_session",
        lambda *a, **k: starts.append(1),
        raising=False,
    )

    with pytest.raises(AVFoundationUnavailable, match="no frames"):
        cap.start()

    assert len(starts) == 2  # every attempt rebuilt the session
    # The registry slot must be released so a later open can claim the camera.
    fresh = mod.NativeAVCapture(device_index=0, width=640, height=360, fps=30.0)
    fresh._register_open_device(_RegDevice("UID_X"))  # must not raise


def test_start_returns_once_first_frame_is_seen(monkeypatch, _clean_registry) -> None:
    from syncfield.adapters import _avfoundation_capture as mod

    cap = mod.NativeAVCapture(
        device_index=0, width=640, height=360, fps=30.0, unique_id="UID_X"
    )
    cap._first_frame_timeout_s = 0.5

    def fake_start_session(*_a, **_k):
        cap._frames_seen += 1  # frames flowing immediately

    monkeypatch.setattr(mod, "_load_frameworks", lambda: (None,) * 6)
    monkeypatch.setattr(
        mod, "_resolve_device", lambda _avf, _uid, _idx: _RegDevice("UID_X")
    )
    monkeypatch.setattr(cap, "_start_session", fake_start_session, raising=False)

    cap.start()

    assert cap._registered_uid == "UID_X"
