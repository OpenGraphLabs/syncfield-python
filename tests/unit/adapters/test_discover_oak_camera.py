"""Unit tests for OakCameraStream.discover()."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_depthai(monkeypatch):
    """Install a fake ``depthai`` so the adapter module imports cleanly."""
    fake = MagicMock()
    # Minimal pipeline / Camera / StereoDepth graph so the rest of
    # oak_camera.py imports without error.
    fake.Pipeline.return_value = MagicMock()
    fake.node = MagicMock()
    fake.node.Camera = object
    fake.ImgFrame.Type.BGR888p = "BGR888p"
    fake.Device.getAllAvailableDevices.return_value = []
    monkeypatch.setitem(sys.modules, "depthai", fake)

    sys.modules.pop("syncfield.adapters.oak_camera", None)
    importlib.import_module("syncfield.adapters.oak_camera")
    yield fake
    sys.modules.pop("syncfield.adapters.oak_camera", None)


def test_discover_empty_device_list(mock_depthai):
    from syncfield.adapters.oak_camera import OakCameraStream

    mock_depthai.Device.getAllAvailableDevices.return_value = []
    assert OakCameraStream.discover() == []


def test_discover_single_device(mock_depthai):
    from syncfield.adapters.oak_camera import OakCameraStream

    mock_depthai.Device.getAllAvailableDevices.return_value = [
        SimpleNamespace(
            name="OAK-D S2",
            deviceId="14442C10517A3ED700",
            state=SimpleNamespace(name="BOOTLOADER"),
        )
    ]
    devices = OakCameraStream.discover()
    assert len(devices) == 1
    device = devices[0]
    assert device.adapter_type == "oak_camera"
    assert device.adapter_cls is OakCameraStream
    assert device.kind == "video"
    assert device.display_name == "OAK-D S2"
    assert device.device_id == "14442C10517A3ED700"
    assert device.construct_kwargs == {"device_id": "14442C10517A3ED700"}
    assert device.accepts_output_dir is True
    assert device.warnings == ()


def test_discover_multiple_devices(mock_depthai):
    from syncfield.adapters.oak_camera import OakCameraStream

    mock_depthai.Device.getAllAvailableDevices.return_value = [
        SimpleNamespace(name="OAK-1", deviceId="AAAA1111", state=None),
        SimpleNamespace(name="OAK-D", deviceId="BBBB2222", state=None),
    ]
    devices = OakCameraStream.discover()
    assert len(devices) == 2
    assert {d.device_id for d in devices} == {"AAAA1111", "BBBB2222"}


def test_discover_swallows_exceptions(mock_depthai):
    from syncfield.adapters.oak_camera import OakCameraStream

    mock_depthai.Device.getAllAvailableDevices.side_effect = RuntimeError("boom")
    # Discovery must never propagate — partial failure semantics are
    # owned by the scanner, not the adapter.
    assert OakCameraStream.discover() == []


def test_class_attributes_for_registry(mock_depthai):
    from syncfield.adapters.oak_camera import OakCameraStream

    assert OakCameraStream._discovery_kind == "video"
    assert OakCameraStream._discovery_adapter_type == "oak_camera"


def test_device_can_be_constructed_from_discovered(mock_depthai, tmp_path):
    from syncfield.adapters.oak_camera import OakCameraStream

    mock_depthai.Device.getAllAvailableDevices.return_value = [
        SimpleNamespace(name="OAK-D", deviceId="XYZ123", state=None),
    ]
    devices = OakCameraStream.discover()
    stream = devices[0].construct(id="oak_main", output_dir=tmp_path)
    assert isinstance(stream, OakCameraStream)
    assert stream._device_id == "XYZ123"  # noqa: SLF001
