"""Unit tests for UVCWebcamStream using a mocked PyAV module."""

from __future__ import annotations

import importlib
import sys
import time

import pytest

from syncfield.clock import SessionClock
from syncfield.types import SyncPoint


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


# ---------------------------------------------------------------------------
# Basic SPI coverage
# ---------------------------------------------------------------------------


def test_capabilities(mock_av, tmp_path):
    from syncfield.adapters.uvc_webcam import UVCWebcamStream

    stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
    assert stream.capabilities.produces_file is True
    assert stream.capabilities.provides_audio_track is False
    assert stream.kind == "video"


def test_prepare_opens_pyav_input(mock_av, tmp_path):
    from syncfield.adapters.uvc_webcam import UVCWebcamStream

    stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
    stream.prepare()

    # av.open was called with the input URL (macOS or Linux depending on
    # the runner), NOT with mode="w".
    input_calls = [
        c for c in mock_av.av.open.call_args_list
        if c.kwargs.get("mode") != "w"
    ]
    assert len(input_calls) == 1


def test_prepare_forwards_pixel_format_to_pyav(mock_av, tmp_path):
    """A requested ``pixel_format`` reaches the avfoundation/v4l2 open options."""
    from syncfield.adapters.uvc_webcam import UVCWebcamStream

    stream = UVCWebcamStream(
        "cam", device_index=0, output_dir=tmp_path, pixel_format="nv12"
    )
    stream.prepare()

    input_calls = [
        c for c in mock_av.av.open.call_args_list
        if c.kwargs.get("mode") != "w"
    ]
    assert len(input_calls) == 1
    assert input_calls[0].kwargs["options"].get("pixel_format") == "nv12"


def test_prepare_falls_back_to_auto_when_pixel_format_rejected(mock_av, tmp_path):
    """If the camera rejects the requested pixel_format, prepare() retries
    once with auto-negotiation (the pre-existing behaviour) instead of
    failing the stream. Guarantees worst-case == today.
    """
    from syncfield.adapters.uvc_webcam import UVCWebcamStream

    input_container = mock_av.input_container

    def _open(url, *args, **kwargs):  # noqa: ANN001 - MagicMock signature
        if kwargs.get("options", {}).get("pixel_format"):
            raise OSError(5, "Input/output error")
        return input_container

    mock_av.av.open.side_effect = _open

    stream = UVCWebcamStream(
        "cam", device_index=0, output_dir=tmp_path, pixel_format="nv12"
    )
    stream.prepare()  # must not raise

    calls = mock_av.av.open.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["options"].get("pixel_format") == "nv12"
    assert calls[1].kwargs["options"].get("pixel_format") is None
    assert stream._input is input_container


def test_output_name_sets_recorded_file_stem(mock_av, tmp_path):
    """The recorded file is named after ``output_name`` (the human alias), while
    the stream is still keyed by its id."""
    from syncfield.adapters.uvc_webcam import UVCWebcamStream

    stream = UVCWebcamStream(
        "uvc_webcam_0", device_index=0, output_dir=tmp_path, output_name="ego_left"
    )
    assert stream._file_path.name == "ego_left.mp4"


def test_set_output_name_relabels_before_recording_and_is_ignored_during(mock_av, tmp_path):
    from syncfield.adapters.uvc_webcam import UVCWebcamStream

    stream = UVCWebcamStream("uvc_webcam_0", device_index=0, output_dir=tmp_path)
    assert stream._file_path.name == "uvc_webcam_0.mp4"  # defaults to id

    stream.set_output_name("ego_right")
    assert stream._file_path.name == "ego_right.mp4"

    # A rename mid-recording must not move the file under the encoder.
    stream._recording = True
    stream.set_output_name("too_late")
    assert stream._file_path.name == "ego_right.mp4"


def test_avfoundation_backend_falls_back_to_pyav_when_unavailable(
    mock_av, tmp_path, monkeypatch
):
    """If the native AVFoundation capture can't start, prepare() downgrades to
    the PyAV backend and opens that — worst case == today's behaviour."""
    from syncfield.adapters import _avfoundation_capture
    from syncfield.adapters.uvc_webcam import UVCWebcamStream

    class _BoomCapture:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise _avfoundation_capture.AVFoundationUnavailable("no camera here")

    monkeypatch.setattr(_avfoundation_capture, "NativeAVCapture", _BoomCapture)

    stream = UVCWebcamStream(
        "cam", device_index=0, output_dir=tmp_path, backend="avfoundation"
    )
    stream.prepare()

    assert stream._backend == "pyav"  # downgraded
    input_calls = [
        c for c in mock_av.av.open.call_args_list if c.kwargs.get("mode") != "w"
    ]
    assert len(input_calls) == 1  # opened the PyAV input instead


def test_start_stop_produces_file_path_in_report(mock_av, tmp_path):
    from syncfield.adapters.uvc_webcam import UVCWebcamStream

    stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
    stream.prepare()
    stream.start(_clock())
    time.sleep(0.1)  # let the background thread drain the decode iterator
    report = stream.stop()
    assert report.status == "completed"
    assert report.file_path is not None
    assert report.frame_count >= 1


def test_av_missing_raises_clear_install_hint(monkeypatch):
    """If PyAV is not installed, importing the video-encoder module
    (and transitively the UVC adapter) raises a hint mentioning the
    ``syncfield[uvc]`` extra.
    """
    monkeypatch.setitem(sys.modules, "av", None)
    sys.modules.pop("syncfield.adapters._video_encoder", None)
    sys.modules.pop("syncfield.adapters.uvc_webcam", None)
    import syncfield.adapters as _adapters_pkg
    monkeypatch.delattr(_adapters_pkg, "_video_encoder", raising=False)
    monkeypatch.delattr(_adapters_pkg, "uvc_webcam", raising=False)
    with pytest.raises(ImportError, match=r"syncfield\[uvc\]"):
        importlib.import_module("syncfield.adapters.uvc_webcam")


# ---------------------------------------------------------------------------
# 4-phase lifecycle — live preview before recording
# ---------------------------------------------------------------------------


class TestFourPhaseLifecycle:
    """UVCWebcamStream must support live preview in CONNECTED state.

    The 4-phase lifecycle is what the viewer uses: ``connect()`` runs
    the capture thread in preview-only mode so ``latest_frame``
    populates before the user clicks Record; ``start_recording()``
    then flips the recording flag and opens the encoder without
    respawning the thread.
    """

    def test_connect_starts_preview_without_writing(
        self, mock_av_generous, tmp_path
    ):
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        time.sleep(0.1)  # let the thread read a few mocked frames
        try:
            # Encoder was never constructed — preview phase doesn't write.
            assert stream._encoder is None  # noqa: SLF001
            # No SampleEvent emissions, no advanced frame counter.
            assert stream._frame_count == 0  # noqa: SLF001
            # But latest_frame IS populated so the viewer card can
            # render the live thumbnail.
            assert stream.latest_frame is not None
        finally:
            stream.disconnect()

    def test_start_recording_flips_to_writing(self, mock_av_generous, tmp_path):
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        time.sleep(0.05)  # preview phase
        frames_before = stream._frame_count  # noqa: SLF001

        stream.start_recording(_clock())
        time.sleep(0.1)  # recording phase

        report = stream.stop_recording()
        try:
            assert frames_before == 0  # preview didn't advance the counter
            # With pace_seconds=0.001 and 0.1s recording window we expect roughly
            # ~100 frames. Assert a lower bound that's meaningful (>5) but a loose
            # upper bound that tolerates CI jitter.
            assert 5 <= report.frame_count <= 10_000
            assert report.file_path is not None
            # Stream stays connected after stop_recording — the thread
            # is still alive so the preview continues.
            assert stream._thread is not None  # noqa: SLF001
            assert stream._thread.is_alive()  # noqa: SLF001
            # Encoder stream was actually called to encode at least once.
            assert mock_av_generous.output_stream.encode.called
        finally:
            stream.disconnect()

    def test_disconnect_stops_capture_thread(self, mock_av_generous, tmp_path):
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        time.sleep(0.05)
        stream.disconnect()
        assert stream._thread is None  # noqa: SLF001

    def test_connect_is_idempotent(self, mock_av_generous, tmp_path):
        """Calling connect() twice must not spawn a second thread."""
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        first_thread = stream._thread  # noqa: SLF001
        stream.connect()  # second call is a no-op
        try:
            assert stream._thread is first_thread  # noqa: SLF001
        finally:
            stream.disconnect()

    def test_jitter_reported_when_enough_frames(
        self, mock_av_generous, tmp_path
    ):
        """After recording 20+ frames, jitter p95/p99 are populated."""
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        stream = UVCWebcamStream(
            "cam", device_index=0, output_dir=tmp_path, fps=30.0
        )
        stream.prepare()
        stream.connect()
        stream.start_recording(_clock())
        # Need enough recorded frames for the >=20 sample threshold.
        # mock_av_generous paces at 1ms/frame, so 100ms should yield ~90.
        time.sleep(0.15)
        report = stream.stop_recording()
        stream.disconnect()

        assert report.jitter_p95_ns is not None
        assert report.jitter_p99_ns is not None
        assert report.jitter_p99_ns >= report.jitter_p95_ns
        # Sanity: jitter should be on the order of pace_seconds (1ms = 1_000_000 ns)
        # — allow generous bounds for CI load.
        assert 0 < report.jitter_p95_ns < 100_000_000  # < 100ms


class TestDecoderResilience:
    """The capture loop must tolerate transient decoder errors.

    AVFoundation on macOS raises ``BlockingIOError`` (EAGAIN, errno 35)
    during camera warmup and occasionally between frames. Linux V4L2
    surfaces the same under EAGAIN=11. Interrupted syscalls (EINTR=4)
    fall in the same bucket. None should kill the capture thread.
    """

    def test_blocking_io_error_does_not_kill_loop(
        self, mock_av_generous, tmp_path
    ):
        """Inject EAGAIN into the decode iterator; loop must keep going."""
        import numpy as np
        from unittest.mock import MagicMock

        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        # A generator that ``raise``s without ever yielding becomes
        # dead after the first next(), so we use an iterator class
        # that keeps state across next() calls: first 3 calls raise
        # EAGAIN, subsequent 50 yield real BGR frames.
        class FlakyIter:
            def __init__(self) -> None:
                self._eagain_left = 3
                self._i = 0

            def __iter__(self):
                return self

            def __next__(self):
                if self._eagain_left > 0:
                    self._eagain_left -= 1
                    raise BlockingIOError(
                        35, "Resource temporarily unavailable", "0"
                    )
                if self._i >= 50:
                    raise StopIteration
                time.sleep(0.001)
                frame = MagicMock(name=f"Frame-{self._i}")
                frame.to_ndarray = MagicMock(
                    return_value=np.full(
                        (48, 64, 3), self._i % 256, dtype=np.uint8
                    )
                )
                self._i += 1
                return frame

        mock_av_generous.input_container.decode = MagicMock(
            return_value=FlakyIter()
        )

        stream = UVCWebcamStream(
            "cam", device_index=0, output_dir=tmp_path, fps=30.0
        )
        stream.prepare()
        stream.connect()
        stream.start_recording(_clock())
        time.sleep(0.1)
        report = stream.stop_recording()
        stream.disconnect()

        assert report.frame_count >= 1
        assert not any(
            "BlockingIOError" in (h.detail or "")
            for h in report.health_events
        )

    def test_fatal_os_error_still_ends_loop(
        self, mock_av_generous, tmp_path
    ):
        """Non-transient OSError (e.g. ENODEV=19) still emits + exits."""
        from unittest.mock import MagicMock

        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        class FatalIter:
            def __iter__(self):
                return self

            def __next__(self):
                raise OSError(19, "No such device", "99")

        mock_av_generous.input_container.decode = MagicMock(
            return_value=FatalIter()
        )

        stream = UVCWebcamStream(
            "cam", device_index=99, output_dir=tmp_path, fps=30.0
        )
        stream.prepare()
        stream.connect()
        time.sleep(0.05)
        stream.disconnect()

        collected = stream._collected_health  # noqa: SLF001
        assert any(
            "No such device" in (h.detail or "") for h in collected
        ), f"expected fatal OSError in health events, got {collected!r}"


class TestRecordingAnchor:
    """Per-recording-window intra-host sync anchor capture.

    UVC webcams have no device clock — ``first_frame_device_ns`` must
    always be ``None``, while ``armed_host_ns`` and
    ``first_frame_host_ns`` are populated on the first recorded frame.
    """

    def test_uvc_anchor_captured_without_device_ts(
        self, mock_av_generous, tmp_path
    ):
        """UVC webcam has no device clock — anchor has armed_ns and
        first_frame_host_ns but first_frame_device_ns is None."""
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        armed_ns = 1_234_567_890
        clock = SessionClock(
            sync_point=SyncPoint.create_now("h"),
            recording_armed_ns=armed_ns,
        )

        stream = UVCWebcamStream(
            "cam", device_index=0, output_dir=tmp_path, fps=30.0
        )
        stream.prepare()
        stream.connect()
        stream.start_recording(clock)
        # Let the capture thread drain at least one frame out of the
        # paced fake iterator.
        time.sleep(0.1)
        report = stream.stop_recording()
        stream.disconnect()

        assert report.recording_anchor is not None
        assert report.recording_anchor.armed_host_ns == armed_ns
        assert report.recording_anchor.first_frame_host_ns >= armed_ns
        # KEY DIFFERENCE from OAK: UVC has no device clock.
        assert report.recording_anchor.first_frame_device_ns is None


def test_device_key_prefers_unique_id(tmp_path):
    from syncfield.adapters.uvc_webcam import UVCWebcamStream

    with_uid = UVCWebcamStream(
        "cam_a", device_index=0, output_dir=tmp_path, unique_id="0xAABB"
    )
    assert with_uid.device_key == ("uvc_webcam", "0xAABB")

    # Legacy streams without a unique_id keep the positional identity.
    without_uid = UVCWebcamStream("cam_b", device_index=3, output_dir=tmp_path)
    assert without_uid.device_key == ("uvc_webcam", "3")


def test_orchestrator_add_rejects_two_streams_on_one_camera(tmp_path):
    """Two streams resolving to one physical camera must not both register."""
    import pytest

    from syncfield.adapters.uvc_webcam import UVCWebcamStream
    from syncfield.orchestrator import SessionOrchestrator

    session = SessionOrchestrator(host_id="test", output_dir=tmp_path)
    session.add(
        UVCWebcamStream("cam_a", device_index=1, output_dir=tmp_path, unique_id="0xSAME")
    )
    with pytest.raises(ValueError, match="already registered"):
        session.add(
            UVCWebcamStream(
                "cam_b", device_index=2, output_dir=tmp_path, unique_id="0xSAME"
            )
        )


class _FakeStallInput:
    """Fake NativeAVCapture: silent until restarted (optionally forever)."""

    def __init__(self, recovers: bool = True) -> None:
        self.restarts = 0
        self._deliver = False
        self._recovers = recovers

    def read(self, timeout: float = 0.5):
        import time as _time

        import numpy as _np

        _time.sleep(0.005)
        if self._deliver:
            return (_np.zeros((2, 2, 3), dtype=_np.uint8), _time.monotonic_ns())
        return None

    def restart(self) -> None:
        self.restarts += 1
        if self._recovers:
            self._deliver = True

    def stop(self) -> None:
        pass


def _run_avf_loop(stream, timeout_s: float = 5.0, until=None):
    import threading
    import time as _time

    thread = threading.Thread(target=stream._capture_loop_avfoundation)
    thread.start()
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        if until is not None and until():
            break
        if not thread.is_alive():
            break
        _time.sleep(0.01)
    stream._stop_event.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_avfoundation_stalled_capture_is_restarted(tmp_path):
    """A silent capture session must be rebuilt, not waited on forever."""
    from syncfield.adapters.uvc_webcam import UVCWebcamStream

    stream = UVCWebcamStream(
        "cam", device_index=0, output_dir=tmp_path, backend="avfoundation"
    )
    stream._stall_timeout_s = 0.05
    stream._input = _FakeStallInput(recovers=True)
    stream._stop_event.clear()

    _run_avf_loop(stream, until=lambda: stream.latest_frame is not None)

    assert stream._input.restarts == 1
    assert stream.latest_frame is not None


def test_avfoundation_stall_watchdog_gives_up_loudly(tmp_path):
    """Restart budget exhausted → health ERROR, loop exits (no zombie stream)."""
    from syncfield.adapters.uvc_webcam import UVCWebcamStream
    from syncfield.types import HealthEventKind

    stream = UVCWebcamStream(
        "cam", device_index=0, output_dir=tmp_path, backend="avfoundation"
    )
    stream._stall_timeout_s = 0.03
    stream._max_silent_restarts = 2
    stream._input = _FakeStallInput(recovers=False)
    stream._stop_event.clear()
    events = []
    stream.on_health(events.append)

    _run_avf_loop(stream)

    assert stream._input.restarts == 2
    errors = [e for e in events if e.kind is HealthEventKind.ERROR]
    assert errors and "giving up" in errors[-1].detail
    reconnects = [e for e in events if e.kind is HealthEventKind.RECONNECT]
    assert len(reconnects) == 2


def test_writes_intrinsic_calibration_sidecar_when_provided(tmp_path):
    """A UVC stream given a CameraCalibration drops {stem}.calibration.json."""
    import json

    from syncfield.adapters.uvc_webcam import UVCWebcamStream
    from syncfield.calibration import CameraCalibration

    calib = CameraCalibration(
        camera_matrix=[[560.0, 0.0, 640.0], [0.0, 560.0, 360.0], [0.0, 0.0, 1.0]],
        distortion_coefficients=[0.1, -0.02, 0.0, 0.0, 0.0],
        resolution=(1280, 720),
        source="measured",
        rms_reprojection_error=0.29,
    )
    stream = UVCWebcamStream(
        "cam", device_index=0, output_dir=tmp_path,
        output_name="ego_center", calibration=calib,
    )
    stream._write_calibration_file()
    path = tmp_path / "ego_center.calibration.json"
    assert path.exists()
    doc = json.loads(path.read_text())
    assert doc["schema"] == "syncfield.camera_calibration.v1"
    assert doc["camera_matrix"][0][0] == 560.0


def test_no_calibration_sidecar_when_absent(tmp_path):
    from syncfield.adapters.uvc_webcam import UVCWebcamStream

    stream = UVCWebcamStream("cam", device_index=0, output_dir=tmp_path)
    stream._write_calibration_file()  # no calibration → no file
    assert not (tmp_path / "cam.calibration.json").exists()
