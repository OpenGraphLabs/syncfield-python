"""Native AVFoundation UVC capture backend (macOS only).

Why this exists
---------------
ffmpeg's ``avfoundation`` demuxer (used by the PyAV backend) gives no control
over the camera's UVC *wire* format and starves multiple cameras. Measured on a
3-camera rig: PyAV delivered ``19 / 6.6 / 4.8`` fps (two cameras collapsing),
while native AVFoundation delivered ``20 / 20 / 31`` fps with no starvation.

The win comes from selecting the camera's high-frame-rate format. A UVC format
that advertises e.g. 720p@120 cannot be uncompressed over USB 2.0 (that would be
~1.5 Gbit/s) — it is **MJPEG on the wire**, which macOS decodes on the host. So
picking the highest-fps format at the requested resolution keeps USB bandwidth
in budget while still delivering decoded frames.

Design
------
``AVCaptureSession`` is callback-driven, so instead of a blocking read loop we
attach an ``AVCaptureVideoDataOutput`` whose delegate converts each frame to a
contiguous BGR ndarray and pushes it onto a small bounded queue. ``UVCWebcamStream``
drains that queue from its existing capture-loop thread, so all downstream
handling (preview ``latest_frame``, MP4 encode, timestamps, sample/health
events) is reused unchanged.

Safety
------
All imports are lazy and guarded. Any failure — PyObjC missing, no device at the
index, unsupported configuration — raises :class:`AVFoundationUnavailable`, which
``UVCWebcamStream`` catches to fall back to the PyAV backend. The worst case is
therefore identical to the previous behaviour.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Process-wide registry of cameras currently opened by a NativeAVCapture.
# macOS happily lets two AVCaptureSessions share one camera, silently
# producing the identical stream twice — the exact one-camera-two-streams
# state the capture pipeline must never enter. The registry turns that
# silent duplication into a loud AVFoundationUnavailable at open time.
_OPEN_UNIQUE_IDS: dict[str, str] = {}
_OPEN_UNIQUE_IDS_LOCK = threading.Lock()

# Screen-capture pseudo devices AVFoundation lists alongside cameras; excluded so
# our positional index matches the app-side ``discover_av_devices`` enumeration.
_PSEUDO_PREFIX = "capture screen"

_DELEGATE_CLS: Any = None  # cached ObjC delegate subclass (built once, lazily)


class AVFoundationUnavailable(RuntimeError):
    """The native AVFoundation backend cannot be used for this device."""


def _load_frameworks() -> tuple[Any, Any, Any, Any, Any, Any]:
    try:
        import objc  # noqa: F401
        import AVFoundation as AVF
        import CoreMedia as CM
        import Quartz
        from Foundation import NSObject
        from libdispatch import dispatch_queue_create
    except Exception as exc:  # noqa: BLE001 - any import problem => unavailable
        raise AVFoundationUnavailable(
            f"PyObjC AVFoundation stack not importable: {exc}"
        ) from exc
    return objc, AVF, CM, Quartz, NSObject, dispatch_queue_create


def bgr_from_pixel_buffer(Quartz: Any, pixel_buffer: Any) -> np.ndarray:
    """Copy a **locked** 32BGRA ``CVPixelBuffer`` into a contiguous BGR ndarray.

    The caller must hold ``CVPixelBufferLockBaseAddress`` for the duration and
    unlock afterwards; we ``copy()`` so the result outlives the lock. Rows may be
    padded (``bytesPerRow`` > ``width*4``), so we reshape by the real stride and
    slice to ``width`` before dropping the alpha channel.
    """
    width = int(Quartz.CVPixelBufferGetWidth(pixel_buffer))
    height = int(Quartz.CVPixelBufferGetHeight(pixel_buffer))
    bytes_per_row = int(Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer))
    base = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
    raw = base.as_buffer(bytes_per_row * height)
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(height, bytes_per_row // 4, 4)
    return arr[:, :width, :3].copy()  # BGRA -> BGR, detached from the locked buffer


def select_capture_format(
    device: Any,
    CM: Any,
    *,
    width: int,
    height: int,
) -> tuple[Any, float, Any]:
    """Choose the capture format and 30-ish-fps cap for ``device``.

    Prefers an exact ``width x height`` match, then same width / closest height,
    then closest overall; among equally-good dimensions it picks the **highest
    max frame rate**, which is the MJPEG-backed (bandwidth-light) format.

    Returns ``(format, max_fps, cap_frame_duration)`` where ``cap_frame_duration``
    is the ``CMTime`` for the target fps to pass to
    ``setActiveVideoMinFrameDuration`` (caps the rate so a well-lit camera doesn't
    blast 120 fps), or ``None`` when no matching range exists.
    """
    best: Optional[tuple[Any, int, int, float]] = None
    best_key: Optional[tuple[Any, ...]] = None
    for fmt in device.formats():
        dims = CM.CMVideoFormatDescriptionGetDimensions(fmt.formatDescription())
        fw, fh = int(dims.width), int(dims.height)
        max_fps = 0.0
        for r in fmt.videoSupportedFrameRateRanges():
            max_fps = max(max_fps, float(r.maxFrameRate()))
        key = (
            fw == width and fh == height,           # exact match wins
            fw == width,                            # then same width
            -(abs(fw - width) + abs(fh - height)),  # then closest dimensions
            max_fps,                                # then highest fps (MJPEG-backed)
        )
        if best_key is None or key > best_key:
            best_key, best = key, (fmt, fw, fh, max_fps)
    if best is None:
        return None, 0.0, None
    fmt, _fw, _fh, max_fps = best
    target = min(30.0, max_fps) if max_fps > 0 else 30.0
    cap_duration = None
    for r in fmt.videoSupportedFrameRateRanges():
        if abs(float(r.maxFrameRate()) - target) < 0.6:
            cap_duration = r.minFrameDuration()
            break
    return fmt, max_fps, cap_duration


def _resolve_device(AVF: Any, unique_id: Optional[str], index: int) -> Any:
    """Resolve the camera to open, preferring the stable ``unique_id``.

    Positional indices are only meaningful within a single enumeration, and the
    SDK's in-process enumeration order does NOT match the app's discovery order,
    so opening by index can select the wrong physical camera — or bind two
    streams to the same one (two visually-identical Arducams). AVFoundation's
    ``deviceWithUniqueID:`` looks the device up directly by its stable id, which
    is the only thing that tells identical cameras apart. Fall back to the
    positional index only when no ``unique_id`` was supplied (legacy path).
    """
    if unique_id:
        device = AVF.AVCaptureDevice.deviceWithUniqueID_(unique_id)
        if device is None:
            raise AVFoundationUnavailable(
                f"camera with unique_id {unique_id!r} not found "
                "(unplugged or moved to a different USB port)"
            )
        return device
    return _device_at_index(AVF, index)


def _device_at_index(AVF: Any, index: int) -> Any:
    """Resolve ``index`` to an ``AVCaptureDevice`` using the same enumeration
    order as the app-side ``discover_av_devices`` (built-in, then external /
    continuity / desk-view, screen-capture pseudo devices excluded)."""
    types = [AVF.AVCaptureDeviceTypeBuiltInWideAngleCamera]
    for name in (
        "AVCaptureDeviceTypeExternal",
        "AVCaptureDeviceTypeExternalUnknown",
        "AVCaptureDeviceTypeContinuityCamera",
        "AVCaptureDeviceTypeDeskViewCamera",
    ):
        constant = getattr(AVF, name, None)
        if constant is not None:
            types.append(constant)
    session = AVF.AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
        types, AVF.AVMediaTypeVideo, 0
    )
    devices = [
        d
        for d in session.devices()
        if not str(d.localizedName() or "").casefold().startswith(_PSEUDO_PREFIX)
    ]
    if 0 <= index < len(devices):
        return devices[index]
    return None


def _delegate_class(objc: Any, NSObject: Any, Quartz: Any, CM: Any) -> Any:
    """Build (once) the ``AVCaptureVideoDataOutputSampleBufferDelegate`` subclass
    that converts frames and hands them to a Python sink callable."""
    global _DELEGATE_CLS
    if _DELEGATE_CLS is not None:
        return _DELEGATE_CLS

    class _SyncfieldFrameDelegate(NSObject):
        def initWithSink_(self, sink: Any) -> Any:
            self = objc.super(_SyncfieldFrameDelegate, self).init()
            if self is None:
                return None
            self._sink = sink
            return self

        def captureOutput_didOutputSampleBuffer_fromConnection_(
            self, _output: Any, sample_buffer: Any, _connection: Any
        ) -> None:
            try:
                image = CM.CMSampleBufferGetImageBuffer(sample_buffer)
                if image is None:
                    return
                Quartz.CVPixelBufferLockBaseAddress(image, 0)
                try:
                    frame_bgr = bgr_from_pixel_buffer(Quartz, image)
                finally:
                    Quartz.CVPixelBufferUnlockBaseAddress(image, 0)
            except Exception:  # noqa: BLE001 - never let a bad frame kill the delegate
                logger.exception("avfoundation.frame_convert_failed")
                return
            self._sink(frame_bgr, time.monotonic_ns())

    _DELEGATE_CLS = _SyncfieldFrameDelegate
    return _DELEGATE_CLS


class NativeAVCapture:
    """Owns an ``AVCaptureSession`` and exposes a ``read()`` that yields
    ``(frame_bgr, capture_ns)`` — a drop-in frame source for the UVC capture loop.
    """

    def __init__(
        self,
        *,
        device_index: int,
        width: int,
        height: int,
        fps: float,
        unique_id: Optional[str] = None,
    ) -> None:
        self._device_index = int(device_index)
        self._unique_id = unique_id or None
        self._width = int(width)
        self._height = int(height)
        self._fps = float(fps)
        self._queue: "queue.Queue[tuple[np.ndarray, int]]" = queue.Queue(maxsize=4)
        self._session: Any = None
        self._delegate: Any = None
        self._dispatch_queue: Any = None
        self._registered_uid: Optional[str] = None
        self._frames_seen = 0
        # First-frame verification knobs — instance attributes so tests can
        # shrink the timeouts.
        self._first_frame_timeout_s = 3.0
        self._open_attempts = 3
        self.selected_max_fps: float = 0.0

    def start(self) -> None:
        """Open the camera and verify it actually delivers frames.

        ``AVCaptureSession`` can come up "running" yet deliver nothing after
        a rapid stop→start on the same camera (observed on macOS 15 when a
        stream reconcile reopens several cameras back-to-back). A session
        that opened without frames is indistinguishable from a healthy one
        by any status API, so the only reliable check is waiting for the
        first frame — and rebuilding the session when it never comes.
        """
        if self._session is not None:
            return
        objc, AVF, CM, Quartz, NSObject, dispatch_queue_create = _load_frameworks()
        device = _resolve_device(AVF, self._unique_id, self._device_index)
        if device is None:
            raise AVFoundationUnavailable(
                f"no AVFoundation camera at index {self._device_index}"
            )
        self._register_open_device(device)
        try:
            for attempt in range(1, self._open_attempts + 1):
                baseline = self._frames_seen
                self._start_session(
                    objc, AVF, CM, Quartz, NSObject, dispatch_queue_create, device
                )
                if self._await_frame_after(baseline, self._first_frame_timeout_s):
                    return
                logger.warning(
                    "avfoundation.no_first_frame unique_id=%s device_index=%s "
                    "attempt=%d/%d — rebuilding capture session",
                    self._unique_id,
                    self._device_index,
                    attempt,
                    self._open_attempts,
                )
                self._teardown_session()
                time.sleep(0.5 * attempt)
            raise AVFoundationUnavailable(
                f"camera unique_id={self._unique_id!r} device_index="
                f"{self._device_index} started but delivered no frames after "
                f"{self._open_attempts} attempts"
            )
        except BaseException:
            self._teardown_session()
            self._unregister_open_device()
            raise

    def _await_frame_after(self, baseline: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._frames_seen > baseline:
                return True
            time.sleep(0.05)
        return self._frames_seen > baseline

    def _register_open_device(self, device: Any) -> None:
        """Claim the resolved camera in the process-wide open registry.

        Registration uses the device's own ``uniqueID`` (not the requested
        ``unique_id``) so positional-index opens are covered too.
        """
        uid = str(device.uniqueID() or "") or None
        if uid is None:
            return
        owner = f"index={self._device_index} name={device.localizedName()!r}"
        with _OPEN_UNIQUE_IDS_LOCK:
            existing = _OPEN_UNIQUE_IDS.get(uid)
            if existing is not None:
                raise AVFoundationUnavailable(
                    f"camera unique_id={uid!r} is already opened in this process "
                    f"({existing}) — refusing to bind the same physical camera "
                    "to a second stream"
                )
            _OPEN_UNIQUE_IDS[uid] = owner
        self._registered_uid = uid

    def _unregister_open_device(self) -> None:
        uid, self._registered_uid = self._registered_uid, None
        if uid is None:
            return
        with _OPEN_UNIQUE_IDS_LOCK:
            _OPEN_UNIQUE_IDS.pop(uid, None)

    def _start_session(
        self,
        objc: Any,
        AVF: Any,
        CM: Any,
        Quartz: Any,
        NSObject: Any,
        dispatch_queue_create: Any,
        device: Any,
    ) -> None:
        fmt, max_fps, cap_duration = select_capture_format(
            device, CM, width=self._width, height=self._height
        )
        session = AVF.AVCaptureSession.alloc().init()
        session.beginConfiguration()
        device_input, err = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(
            device, None
        )
        if device_input is None or not session.canAddInput_(device_input):
            raise AVFoundationUnavailable(f"cannot add camera input: {err}")
        session.addInput_(device_input)

        output = AVF.AVCaptureVideoDataOutput.alloc().init()
        output.setAlwaysDiscardsLateVideoFrames_(True)
        output.setVideoSettings_(
            {Quartz.kCVPixelBufferPixelFormatTypeKey: Quartz.kCVPixelFormatType_32BGRA}
        )
        delegate_cls = _delegate_class(objc, NSObject, Quartz, CM)
        self._delegate = delegate_cls.alloc().initWithSink_(self._on_frame)
        self._dispatch_queue = dispatch_queue_create(b"syncfield.uvc.avfoundation", None)
        output.setSampleBufferDelegate_queue_(self._delegate, self._dispatch_queue)
        if not session.canAddOutput_(output):
            raise AVFoundationUnavailable("cannot add AVCaptureVideoDataOutput")
        session.addOutput_(output)
        session.commitConfiguration()

        if fmt is not None and device.lockForConfiguration_(None):
            try:
                device.setActiveFormat_(fmt)
                # Cap the frame RATE (min frame DURATION) so a bright scene can't
                # push 120 fps; leave the max duration unset so the camera may
                # still drop below target in low light rather than error.
                if cap_duration is not None:
                    device.setActiveVideoMinFrameDuration_(cap_duration)
            finally:
                device.unlockForConfiguration()

        session.startRunning()
        self._session = session
        self.selected_max_fps = max_fps
        logger.info(
            "avfoundation.capture.started unique_id=%s device_index=%s size=%sx%s fmt_max_fps=%.0f",
            self._unique_id,
            self._device_index,
            self._width,
            self._height,
            max_fps,
        )

    def _on_frame(self, frame_bgr: np.ndarray, capture_ns: int) -> None:
        self._frames_seen += 1
        try:
            self._queue.put_nowait((frame_bgr, capture_ns))
        except queue.Full:
            # Preview/record can't keep up momentarily — drop the oldest frame so
            # latency stays bounded rather than growing without limit.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait((frame_bgr, capture_ns))
            except queue.Full:
                pass

    def read(self, timeout: float = 0.5) -> Optional[tuple[np.ndarray, int]]:
        """Block up to ``timeout`` for the next ``(frame_bgr, capture_ns)``; return
        ``None`` on timeout so the caller can re-check its stop flag."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _teardown_session(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.stopRunning()
            except Exception:  # noqa: BLE001 - teardown must not raise
                logger.debug("avfoundation.stop_running_failed")
        self._delegate = None
        self._dispatch_queue = None

    def stop(self) -> None:
        self._teardown_session()
        self._unregister_open_device()

    def restart(self) -> None:
        """Tear the capture down and reopen it (first-frame verified).

        Used by the capture loop to recover a stalled camera — a session
        that stops delivering frames mid-run never comes back on its own,
        but a fresh session on the same device does (verified live).
        """
        self.stop()
        time.sleep(0.5)
        self.start()
