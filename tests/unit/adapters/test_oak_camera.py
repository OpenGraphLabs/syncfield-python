"""Unit tests for OakCameraStream using a mocked depthai module."""

from __future__ import annotations

import importlib
import sys
import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from syncfield.clock import SessionClock
from syncfield.types import SyncPoint


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


def _build_fake_depthai() -> MagicMock:
    """Return a MagicMock that looks enough like depthai for the adapter.

    Models the depthai v3 pipeline API: Pipeline.build() / start() / stop(),
    Camera node with requestOutput() → OutputQueue.get() → ImgFrame-like
    object exposing .getCvFrame() and .getTimestamp().

    Covers both OAK encoding modes:

    * ``"raw"`` legacy path — Camera → BGR frames direct into the
      capture loop. The main ``rgb_queue`` yields ``_FakeFrame`` objects
      whose ``.getCvFrame()`` returns a real 1080p zero BGR array.
    * ``"h264"`` default path — Camera → VideoEncoder → encoded packets
      that expose ``.getData()`` returning a short fake Annex-B buffer,
      plus a parallel low-res BGR preview queue. ``pipeline.create`` is
      given a side_effect so the VideoEncoder node is a distinct mock
      and exposes a distinct ``.out.createOutputQueue`` wired to the
      encoded-packet queue.

    The fake queues return unlimited streams — in the 4-phase lifecycle
    the capture loop runs across both preview (``connect()``) and
    recording (``start_recording()``), so a fixed budget would get
    consumed before any recording happens. Tests that want to exercise
    "queue drains" swap in a narrower side_effect manually.
    """
    fake = MagicMock()

    # --- Fake frame object with numpy-shaped data ------------------------
    class _FakeFrame:
        def __init__(self) -> None:
            # Real BGR ndarray so the real VideoEncoder.write() path
            # (via av.VideoFrame.from_ndarray) accepts it cleanly.
            self._cv_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

        def getCvFrame(self) -> np.ndarray:
            return self._cv_frame

    class _FakeEncodedPacket:
        """Mimics a dai ``EncodedFrame`` with a short fake Annex-B payload.

        The exact bytes don't matter for unit tests — the adapter only
        writes them to a raw file and (in real hardware runs) lets PyAV
        demux them. The on-device test relies on real bitstreams.
        """

        _FAKE_NAL = b"\x00\x00\x00\x01\x67\x42\x00\x00"  # SPS start marker

        def getData(self) -> bytes:
            return self._FAKE_NAL

    # --- Fake output queues: unlimited streams --------------------------
    def make_queue(payload_factory) -> MagicMock:
        q = MagicMock()
        q.get.side_effect = lambda timeout=0.1: payload_factory()
        q.tryGet.return_value = None
        return q

    rgb_queue = make_queue(_FakeFrame)  # raw mode main queue
    encoded_queue = make_queue(_FakeEncodedPacket)  # h264 main queue
    preview_queue = make_queue(_FakeFrame)  # h264 preview queue
    preview_queue.tryGet.side_effect = lambda: _FakeFrame()

    # --- Fake Camera node -----------------------------------------------
    camera_node = MagicMock()
    # Each requestOutput() call returns the same Node.Output mock whose
    # createOutputQueue() returns ``rgb_queue`` in raw mode and
    # ``preview_queue`` in h264 mode (since the encoder path uses the
    # encoder's own ``.out`` for its queue, this queue serves the
    # viewer preview branch). The adapter calls createOutputQueue once
    # per branch; MagicMock returns the same value on repeat calls.
    camera_output = MagicMock(name="CameraOutput")
    camera_node.requestOutput.return_value = camera_output
    # Raw mode's queue; h264 preview mode reuses the same factory
    # because the adapter only reads via ``getCvFrame`` / ``tryGet``.
    camera_output.createOutputQueue.return_value = rgb_queue

    # --- Fake VideoEncoder node -----------------------------------------
    encoder_node = MagicMock(name="VideoEncoder")
    encoder_node.out.createOutputQueue.return_value = encoded_queue

    # --- Fake StereoDepth node ------------------------------------------
    stereo_node = MagicMock(name="StereoDepth")

    # --- Fake pipeline dispatch on node class ---------------------------
    pipeline = MagicMock()

    def _pipeline_create(node_cls):  # noqa: ANN001 - MagicMock signature
        if node_cls is fake.node.VideoEncoder:
            # Reroute the Camera→encoder link target: if the adapter
            # later calls createOutputQueue on ``camera_output`` while
            # building the h264 branch, reroute it to ``preview_queue``
            # so the preview branch gets a distinct queue from the
            # encoded one. First call was already for the encoder
            # input; subsequent calls are for the preview branch.
            camera_output.createOutputQueue.return_value = preview_queue
            return encoder_node
        if node_cls is fake.node.StereoDepth:
            return stereo_node
        return camera_node

    pipeline.create.side_effect = _pipeline_create
    pipeline.getDefaultDevice.return_value.getUsbSpeed.return_value = MagicMock(
        name="SUPER", value=3
    )

    fake.Pipeline.return_value = pipeline

    # dai.node namespace — sentinels used in pipeline.create() dispatch.
    # Using small concrete types keeps ``is`` comparisons stable across
    # repeated attribute access. StereoDepth needs a nested ``PresetMode``
    # attribute because the adapter reads ``PresetMode.HIGH_DETAIL`` when
    # wiring the depth branch.
    fake.node = MagicMock()
    fake.node.Camera = type("Camera", (), {})
    fake.node.VideoEncoder = type("VideoEncoder", (), {})

    class _FakeStereoDepth:
        class PresetMode:
            HIGH_DETAIL = object()

    fake.node.StereoDepth = _FakeStereoDepth

    # dai.Device.getAllAvailableDevices()
    fake.Device.getAllAvailableDevices.return_value = [MagicMock()]

    # dai.ImgFrame.Type sentinels
    fake.ImgFrame.Type.BGR888p = "BGR888p"
    fake.ImgFrame.Type.NV12 = "NV12"

    # dai.VideoEncoderProperties.Profile sentinels
    fake.VideoEncoderProperties.Profile.H264_MAIN = "H264_MAIN"

    # dai.UsbSpeed.SUPER sentinel (for USB-speed warning branch)
    fake.UsbSpeed.SUPER = MagicMock(value=3)

    return fake


@pytest.fixture
def mock_depthai(monkeypatch):
    """Patch ``sys.modules['depthai']`` with a fake pipeline.

    OAK tests that exercise the recording path must also inject
    ``mock_av_generous`` (from ``conftest.py``) so the shared
    ``VideoEncoder`` sees a fake ``av`` module.
    """
    fake = _build_fake_depthai()
    monkeypatch.setitem(sys.modules, "depthai", fake)
    sys.modules.pop("syncfield.adapters.oak_camera", None)
    yield fake
    sys.modules.pop("syncfield.adapters.oak_camera", None)


class TestCapabilities:
    def test_capabilities(self, mock_depthai, mock_av_generous, tmp_path):
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        assert stream.capabilities.produces_file is True
        assert stream.capabilities.provides_audio_track is False
        assert stream.capabilities.is_removable is True
        assert stream.capabilities.supports_precise_timestamps is True
        assert stream.kind == "video"


class TestLifecycle:
    """Exercise the 4-phase connect → start_recording → stop_recording → disconnect path.

    In 0.2 the pipeline build moved from ``prepare()`` into
    ``connect()`` so the viewer can show a live preview before Record
    is pressed. These tests pin that split down.
    """

    def test_connect_builds_and_starts_pipeline(
        self, mock_depthai, mock_av_generous, tmp_path
    ):
        fake = mock_depthai
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.prepare()
        # prepare() no longer opens the pipeline — only connect() does.
        assert fake.Pipeline.call_count == 0

        stream.connect()
        # Give the capture thread a moment to pull a frame or two.
        time.sleep(0.05)
        try:
            fake.Pipeline.assert_called_once()
            pipeline = fake.Pipeline.return_value
            assert pipeline.build.called
            assert pipeline.start.called
        finally:
            stream.disconnect()

    def test_connect_raises_when_no_devices(
        self, mock_depthai, mock_av_generous, tmp_path
    ):
        fake = mock_depthai
        fake.Device.getAllAvailableDevices.return_value = []
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.prepare()
        with pytest.raises(RuntimeError, match="No OAK devices"):
            stream.connect()

    def test_full_lifecycle_produces_file_path(
        self, mock_depthai, mock_av_generous, tmp_path
    ):
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        stream.start_recording(_clock())
        # Give the background thread time to read the mocked frames
        time.sleep(0.15)
        report = stream.stop_recording()
        stream.disconnect()

        assert report.status == "completed"
        assert report.file_path is not None
        assert report.frame_count >= 1

    def test_disconnect_releases_pipeline(
        self, mock_depthai, mock_av_generous, tmp_path
    ):
        fake = mock_depthai
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        time.sleep(0.05)
        stream.disconnect()

        pipeline = fake.Pipeline.return_value
        assert pipeline.stop.called

    def test_legacy_start_stop_still_works(
        self, mock_depthai, mock_av_generous, tmp_path
    ):
        """Old 0.1-era ``prepare() → start() → stop()`` path stays valid."""
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.prepare()
        stream.start(_clock())
        time.sleep(0.15)
        report = stream.stop()

        assert report.status == "completed"
        assert report.file_path is not None
        assert report.frame_count >= 1


class TestDepthOption:
    def test_depth_enabled_declares_depth_output(
        self, mock_depthai, mock_av_generous, tmp_path
    ):
        """When depth_enabled=True the pipeline builds a StereoDepth node."""
        fake = mock_depthai
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream(
            "oak_d",
            output_dir=tmp_path,
            depth_enabled=True,
        )
        stream.prepare()
        stream.connect()
        try:
            # pipeline.create was called twice (Camera + StereoDepth)
            pipeline = fake.Pipeline.return_value
            assert pipeline.create.call_count >= 2
        finally:
            stream.disconnect()

    def test_depth_disabled_by_default(
        self, mock_depthai, mock_av_generous, tmp_path
    ):
        """With depth disabled no StereoDepth node is added.

        Pinned to ``encoding="raw"`` to keep the call-count assertion
        focused on the depth branch — h264 mode adds a VideoEncoder
        node that would otherwise confuse the count.
        """
        fake = mock_depthai
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path, encoding="raw")
        stream.prepare()
        stream.connect()
        try:
            pipeline = fake.Pipeline.return_value
            assert pipeline.create.call_count == 1  # RGB only
        finally:
            stream.disconnect()


class TestEncoding:
    """Exercise the ``encoding`` parameter — h264 (default) vs raw."""

    def test_h264_default_builds_video_encoder_branch(
        self, mock_depthai, mock_av_generous, tmp_path
    ):
        """Default encoding wires a VideoEncoder node + preview branch.

        Both nodes are created via ``pipeline.create``, so we check
        that both class sentinels were passed through. The camera
        itself is also one create call, bringing the total to three
        when depth is enabled and two when not.
        """
        fake = mock_depthai
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        assert stream._encoding == "h264"

        stream.connect()
        try:
            pipeline = fake.Pipeline.return_value
            created_types = [
                call.args[0] for call in pipeline.create.call_args_list
            ]
            assert fake.node.Camera in created_types
            assert fake.node.VideoEncoder in created_types
        finally:
            stream.disconnect()

    def test_h264_recording_produces_mp4_via_remux(
        self, mock_depthai, mock_av_generous, tmp_path
    ):
        """Full lifecycle in h264 mode yields a FinalizationReport with
        ``file_path`` set to the target MP4.

        The raw ``.h264`` intermediate file is created during the
        recording window; after :meth:`stop_recording` runs the remux
        it should be gone. The mocked ``av`` pipeline completes the
        remux with an empty packet stream, which is enough to verify
        the control-flow works end-to-end.
        """
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.connect()
        stream.start_recording(_clock())

        h264_path = tmp_path / "oak.h264"
        # Capture thread should open the intermediate bitstream file
        # almost immediately after start_recording flips the flag.
        # Give it a beat to produce a few samples.
        time.sleep(0.15)

        report = stream.stop_recording()
        stream.disconnect()

        assert report.status == "completed"
        assert report.file_path == tmp_path / "oak.mp4"
        # Remux succeeded under the mock, so the intermediate file
        # should have been cleaned up.
        assert not h264_path.exists()

    def test_raw_mode_opt_in_keeps_legacy_pipeline(
        self, mock_depthai, mock_av_generous, tmp_path
    ):
        """Explicit ``encoding="raw"`` should skip the VideoEncoder."""
        fake = mock_depthai
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path, encoding="raw")
        stream.connect()
        try:
            pipeline = fake.Pipeline.return_value
            created_types = [
                call.args[0] for call in pipeline.create.call_args_list
            ]
            assert fake.node.Camera in created_types
            assert fake.node.VideoEncoder not in created_types
        finally:
            stream.disconnect()

    def test_invalid_encoding_raises(self, mock_depthai, mock_av_generous, tmp_path):
        from syncfield.adapters.oak_camera import OakCameraStream

        with pytest.raises(ValueError, match="encoding"):
            OakCameraStream("oak", output_dir=tmp_path, encoding="vp9")


class TestImportGuard:
    def test_depthai_missing_raises_clear_install_hint(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "depthai", None)
        sys.modules.pop("syncfield.adapters.oak_camera", None)
        import syncfield.adapters as _pkg
        monkeypatch.delattr(_pkg, "oak_camera", raising=False)
        with pytest.raises(ImportError, match=r"syncfield\[oak\]"):
            importlib.import_module("syncfield.adapters.oak_camera")


class TestDeviceShutterHostNs:
    """``_device_shutter_host_ns`` projects the DepthAI on-device shutter
    instant onto the host monotonic clock, as integer nanoseconds.

    It is called on every frame before the message reaches any handler,
    so the contract is: never raise, return ``None`` on any form of
    missing / malformed timestamp, and use integer arithmetic to avoid
    float rounding at the ~10¹⁸ ns magnitudes reached after long uptimes.
    """

    def _load_helper(self, mock_depthai):  # noqa: ARG002 - fixture runs for side effects
        from syncfield.adapters.oak_camera import _device_shutter_host_ns
        return _device_shutter_host_ns

    def test_converts_timedelta_to_nanoseconds(self, mock_depthai):
        from datetime import timedelta

        helper = self._load_helper(mock_depthai)
        msg = MagicMock()
        # 5 s, 500 μs — a realistic pipeline-warmup-era value.
        msg.getTimestamp.return_value = timedelta(seconds=5, microseconds=500)

        assert helper(msg) == 5_000_500_000

    def test_preserves_nanosecond_precision_for_large_values(self, mock_depthai):
        """Integer math survives datetime's microsecond resolution AND the
        ~10¹⁸ ns range reached after multi-day uptime."""
        from datetime import timedelta

        helper = self._load_helper(mock_depthai)
        msg = MagicMock()
        msg.getTimestamp.return_value = timedelta(days=1, seconds=2, microseconds=3)

        # (86400 + 2) * 10⁹  +  3 * 10³
        expected = (86_400 + 2) * 1_000_000_000 + 3 * 1_000
        assert helper(msg) == expected

    def test_none_message_returns_none(self, mock_depthai):
        assert self._load_helper(mock_depthai)(None) is None

    def test_get_timestamp_raising_returns_none(self, mock_depthai):
        helper = self._load_helper(mock_depthai)
        msg = MagicMock()
        msg.getTimestamp.side_effect = RuntimeError("not ready yet")

        assert helper(msg) is None

    def test_get_timestamp_returning_none_returns_none(self, mock_depthai):
        helper = self._load_helper(mock_depthai)
        msg = MagicMock()
        msg.getTimestamp.return_value = None

        assert helper(msg) is None


class TestRecordingAnchor:
    """Per-recording-window intra-host sync anchor capture.

    OAK cameras expose a real on-device shutter timestamp, so the
    anchor's ``first_frame_device_ns`` is expected to be populated
    (non-``None``) on the very first frame that arrives after
    ``start_recording()``.
    """

    def test_anchor_captured_from_first_frame(
        self, mock_depthai, mock_av_generous, tmp_path, monkeypatch
    ):
        """After arming the clock and pushing frames through the fake
        pipeline, ``stop_recording`` returns a :class:`FinalizationReport`
        whose ``recording_anchor`` captures the armed host ns, the host
        arrival ns of the first recorded frame, and the device-clock
        timestamp of that frame."""
        from syncfield.adapters import oak_camera
        from syncfield.adapters.oak_camera import OakCameraStream

        # Force a known, non-None device timestamp on every frame so the
        # assertion on ``first_frame_device_ns`` is unambiguous. The
        # fake ``_FakeFrame`` / ``_FakeEncodedPacket`` payloads used by
        # the shared fixture don't define ``getTimestamp``, so the real
        # helper would return ``None``.
        known_device_ns = 42_000_000_000
        monkeypatch.setattr(
            oak_camera, "_device_timestamp_ns", lambda msg: known_device_ns
        )

        armed_ns = 1_234_567_890
        clock = SessionClock(
            sync_point=SyncPoint.create_now("h"),
            recording_armed_ns=armed_ns,
        )

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        stream.start_recording(clock)
        # Let the capture thread drain at least one frame out of the
        # unlimited fake queue.
        time.sleep(0.15)
        report = stream.stop_recording()
        stream.disconnect()

        assert report.recording_anchor is not None
        assert report.recording_anchor.armed_host_ns == armed_ns
        assert report.recording_anchor.first_frame_host_ns >= armed_ns
        assert report.recording_anchor.first_frame_device_ns == known_device_ns

    def test_no_anchor_when_no_frames_arrive(
        self, mock_depthai, mock_av_generous, tmp_path
    ):
        """If ``start_recording`` is called but zero frames arrive before
        ``stop_recording``, the report's ``recording_anchor`` stays
        ``None``."""
        from syncfield.adapters.oak_camera import OakCameraStream

        armed_ns = 9_876_543_210
        clock = SessionClock(
            sync_point=SyncPoint.create_now("h"),
            recording_armed_ns=armed_ns,
        )

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        # Starve the capture loop so the recording window sees no frames.
        # ``_safe_get_rgb`` swallows exceptions and returns ``None``, so
        # raising from ``get`` keeps the loop spinning without producing
        # a frame. Set BEFORE ``start_recording`` and give the capture
        # thread a beat to settle on the new side_effect so no in-flight
        # frame slips past the ``_recording`` flag flip.
        stream._q_rgb.get.side_effect = RuntimeError("starved")
        time.sleep(0.05)
        stream.start_recording(clock)
        time.sleep(0.1)
        report = stream.stop_recording()
        stream.disconnect()

        assert report.recording_anchor is None
