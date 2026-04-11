"""Unit tests for OakCameraStream using a mocked depthai module."""

from __future__ import annotations

import importlib
import sys
import time
from unittest.mock import MagicMock

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

    The fake queue returns an unlimited stream of frames — in the new
    4-phase lifecycle the capture loop runs across both the preview
    (``connect()``) and recording (``start_recording()``) phases, so a
    fixed budget would get consumed before any recording happens.
    Tests that want to exercise "queue drains" swap in a narrower
    side_effect manually.
    """
    fake = MagicMock()

    # --- Fake frame object with numpy-shaped data ------------------------
    class _FakeFrame:
        def __init__(self) -> None:
            self._cv_frame = MagicMock()
            self._cv_frame.shape = (1080, 1920, 3)

        def getCvFrame(self) -> MagicMock:
            return self._cv_frame

    # --- Fake output queue: unlimited stream of frames ------------------
    def make_queue() -> MagicMock:
        q = MagicMock()

        def fake_get(timeout: float = 0.1) -> _FakeFrame:
            return _FakeFrame()

        q.get.side_effect = fake_get
        q.tryGet.return_value = None
        return q

    rgb_queue = make_queue()

    # --- Fake Camera node -----------------------------------------------
    camera_node = MagicMock()
    camera_node.requestOutput.return_value.createOutputQueue.return_value = rgb_queue

    # --- Fake pipeline: pipeline.create(dai.node.Camera) returns camera -
    pipeline = MagicMock()
    pipeline.create.return_value = camera_node
    pipeline.getDefaultDevice.return_value.getUsbSpeed.return_value = MagicMock(
        name="SUPER", value=3
    )

    fake.Pipeline.return_value = pipeline

    # dai.node namespace
    fake.node = MagicMock()
    fake.node.Camera = object  # sentinel class passed to pipeline.create

    # dai.Device.getAllAvailableDevices()
    fake.Device.getAllAvailableDevices.return_value = [MagicMock()]

    # dai.ImgFrame.Type.BGR888p sentinel
    fake.ImgFrame.Type.BGR888p = "BGR888p"

    # dai.UsbSpeed.SUPER sentinel (for USB-speed warning branch)
    fake.UsbSpeed.SUPER = MagicMock(value=3)

    return fake


@pytest.fixture
def mock_depthai(monkeypatch):
    fake = _build_fake_depthai()
    monkeypatch.setitem(sys.modules, "depthai", fake)
    # Also mock cv2 since the adapter uses it for the VideoWriter
    fake_cv2 = MagicMock()
    fake_cv2.VideoWriter_fourcc = lambda *args: 0
    fake_cv2.VideoWriter.return_value = MagicMock()
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    # Force re-import so the adapter binds to the fake modules
    sys.modules.pop("syncfield.adapters.oak_camera", None)
    importlib.import_module("syncfield.adapters.oak_camera")
    yield fake, fake_cv2
    sys.modules.pop("syncfield.adapters.oak_camera", None)


class TestCapabilities:
    def test_capabilities(self, mock_depthai, tmp_path):
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

    def test_connect_builds_and_starts_pipeline(self, mock_depthai, tmp_path):
        fake, _ = mock_depthai
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

    def test_connect_raises_when_no_devices(self, mock_depthai, tmp_path):
        fake, _ = mock_depthai
        fake.Device.getAllAvailableDevices.return_value = []
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.prepare()
        with pytest.raises(RuntimeError, match="No OAK devices"):
            stream.connect()

    def test_full_lifecycle_produces_file_path(self, mock_depthai, tmp_path):
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

    def test_disconnect_releases_pipeline(self, mock_depthai, tmp_path):
        fake, _ = mock_depthai
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        time.sleep(0.05)
        stream.disconnect()

        pipeline = fake.Pipeline.return_value
        assert pipeline.stop.called

    def test_legacy_start_stop_still_works(self, mock_depthai, tmp_path):
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
    def test_depth_enabled_declares_depth_output(self, mock_depthai, tmp_path):
        """When depth_enabled=True the pipeline builds a StereoDepth node."""
        fake, _ = mock_depthai
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

    def test_depth_disabled_by_default(self, mock_depthai, tmp_path):
        """Default config builds only the RGB camera node."""
        fake, _ = mock_depthai
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.prepare()
        stream.connect()
        try:
            pipeline = fake.Pipeline.return_value
            assert pipeline.create.call_count == 1  # RGB only
        finally:
            stream.disconnect()


class TestImportGuard:
    def test_depthai_missing_raises_clear_install_hint(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "depthai", None)
        sys.modules.pop("syncfield.adapters.oak_camera", None)
        with pytest.raises(ImportError, match=r"syncfield\[oak\]"):
            importlib.import_module("syncfield.adapters.oak_camera")
