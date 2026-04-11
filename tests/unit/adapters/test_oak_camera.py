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


def _build_fake_depthai(frame_budget: int = 3) -> MagicMock:
    """Return a MagicMock that looks enough like depthai for the adapter.

    Models the depthai v3 pipeline API: Pipeline.build() / start() / stop(),
    Camera node with requestOutput() → OutputQueue.get() → ImgFrame-like
    object exposing .getCvFrame() and .getTimestamp().
    """
    fake = MagicMock()

    # --- Fake frame object with numpy-shaped data ------------------------
    class _FakeFrame:
        def __init__(self) -> None:
            self._cv_frame = MagicMock()
            self._cv_frame.shape = (1080, 1920, 3)

        def getCvFrame(self) -> MagicMock:
            return self._cv_frame

    # --- Fake output queue: returns a few frames then None --------------
    call_count = {"n": 0}

    def make_queue() -> MagicMock:
        q = MagicMock()

        def fake_get(timeout: float = 0.1) -> _FakeFrame | None:
            call_count["n"] += 1
            if call_count["n"] <= frame_budget:
                return _FakeFrame()
            return None

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
    def test_prepare_builds_and_starts_pipeline(self, mock_depthai, tmp_path):
        fake, _ = mock_depthai
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.prepare()

        # Pipeline was constructed, built, and started
        fake.Pipeline.assert_called_once()
        pipeline = fake.Pipeline.return_value
        assert pipeline.build.called
        assert pipeline.start.called

    def test_prepare_raises_when_no_devices(self, mock_depthai, tmp_path):
        fake, _ = mock_depthai
        fake.Device.getAllAvailableDevices.return_value = []
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        with pytest.raises(RuntimeError, match="No OAK devices"):
            stream.prepare()

    def test_start_stop_produces_file_path(self, mock_depthai, tmp_path):
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.prepare()
        stream.start(_clock())
        # Give the background thread time to read the mocked frames
        time.sleep(0.15)
        report = stream.stop()

        assert report.status == "completed"
        assert report.file_path is not None
        assert report.frame_count >= 1

    def test_stop_releases_pipeline(self, mock_depthai, tmp_path):
        fake, _ = mock_depthai
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.prepare()
        stream.start(_clock())
        time.sleep(0.05)
        stream.stop()

        pipeline = fake.Pipeline.return_value
        assert pipeline.stop.called


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

        # pipeline.create was called twice (Camera + StereoDepth)
        pipeline = fake.Pipeline.return_value
        assert pipeline.create.call_count >= 2

    def test_depth_disabled_by_default(self, mock_depthai, tmp_path):
        """Default config builds only the RGB camera node."""
        fake, _ = mock_depthai
        from syncfield.adapters.oak_camera import OakCameraStream

        stream = OakCameraStream("oak", output_dir=tmp_path)
        stream.prepare()

        pipeline = fake.Pipeline.return_value
        assert pipeline.create.call_count == 1  # RGB only


class TestImportGuard:
    def test_depthai_missing_raises_clear_install_hint(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "depthai", None)
        sys.modules.pop("syncfield.adapters.oak_camera", None)
        with pytest.raises(ImportError, match=r"syncfield\[oak\]"):
            importlib.import_module("syncfield.adapters.oak_camera")
