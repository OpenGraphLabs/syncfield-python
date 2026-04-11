"""Unit tests for UVCWebcamStream.discover() — platform-specific enumeration.

Exercises the macOS + Linux branches in isolation via subprocess /
filesystem mocks. The unsupported-platform branch also has a test so
Windows users see a predictable empty list instead of an exception.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_cv2(monkeypatch):
    """Install a fake ``cv2`` so uvc_webcam.py imports cleanly."""
    fake = MagicMock()
    fake.VideoCapture.return_value = MagicMock(isOpened=lambda: True)
    fake.VideoWriter_fourcc = lambda *a: 0
    monkeypatch.setitem(sys.modules, "cv2", fake)
    sys.modules.pop("syncfield.adapters.uvc_webcam", None)
    importlib.import_module("syncfield.adapters.uvc_webcam")
    yield fake
    sys.modules.pop("syncfield.adapters.uvc_webcam", None)


class TestMacosBranch:
    def test_parses_system_profiler_output(self, mock_cv2):
        from syncfield.adapters import uvc_webcam
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        fake_output = {
            "SPCameraDataType": [
                {
                    "_name": "FaceTime HD Camera (Built-in)",
                    "spcamera_model-id": "UVC Camera VendorID_0x05AC",
                },
                {
                    "_name": "Logitech Brio",
                    "spcamera_model-id": "UVC Camera VendorID_0x046D",
                },
            ]
        }

        with patch.object(sys, "platform", "darwin"), patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=0, stdout=json.dumps(fake_output)
            ),
        ):
            devices = UVCWebcamStream.discover()

        assert len(devices) == 2
        assert devices[0].display_name == "FaceTime HD Camera (Built-in)"
        assert devices[0].device_id == "0"
        assert devices[0].construct_kwargs == {"device_index": 0}
        assert devices[0].accepts_output_dir is True
        assert devices[1].display_name == "Logitech Brio"
        assert devices[1].construct_kwargs == {"device_index": 1}

    def test_missing_system_profiler_returns_empty(self, mock_cv2):
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        with patch.object(sys, "platform", "darwin"), patch(
            "subprocess.run", side_effect=FileNotFoundError("no tool")
        ):
            assert UVCWebcamStream.discover() == []

    def test_system_profiler_failure_returns_empty(self, mock_cv2):
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        with patch.object(sys, "platform", "darwin"), patch(
            "subprocess.run",
            return_value=MagicMock(returncode=1, stdout=""),
        ):
            assert UVCWebcamStream.discover() == []

    def test_malformed_json_returns_empty(self, mock_cv2):
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        with patch.object(sys, "platform", "darwin"), patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="not json"),
        ):
            assert UVCWebcamStream.discover() == []


class TestLinuxBranch:
    def test_enumerates_dev_video(self, mock_cv2, tmp_path):
        from syncfield.adapters import uvc_webcam
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        # Simulated /dev directory with two video files
        dev = tmp_path / "dev"
        dev.mkdir()
        (dev / "video0").touch()
        (dev / "video1").touch()
        (dev / "video10").touch()  # make sure numeric sort beats alpha

        def fake_read_text(self):
            mapping = {
                "video0": "Integrated Camera",
                "video1": "HD Webcam C920",
                "video10": "Secondary Camera",
            }
            return mapping.get(self.parent.name, "")

        def fake_exists(self):
            name = self.name if hasattr(self, "name") else ""
            if str(self).startswith("/dev") and name in {"/dev", "dev"}:
                return True
            # sysfs paths
            if "sys/class/video4linux" in str(self):
                return True
            return Path.exists.__wrapped__(self) if hasattr(Path.exists, "__wrapped__") else True

        # Monkey-patch the Path used inside _discover_uvc_linux
        original_path_class = uvc_webcam.__dict__.get("_Path", Path)

        class _FakePath(type(dev)):  # type: ignore[misc]
            """Path subclass that redirects /dev and /sys reads to the tmp tree."""

            def __new__(cls, *args, **kwargs):
                # Reroute absolute paths we care about.
                if args and args[0] == "/dev":
                    return type(dev)(dev)
                return type(dev)(*args, **kwargs)  # type: ignore[misc]

        # Simpler: patch the module-level helper directly to short-circuit
        with patch.object(sys, "platform", "linux"), patch.object(
            uvc_webcam,
            "_discover_uvc_linux",
            return_value=[
                {"index": 0, "name": "Integrated Camera", "description": "uvc · /dev/video0"},
                {"index": 1, "name": "HD Webcam C920", "description": "uvc · /dev/video1"},
            ],
        ):
            devices = UVCWebcamStream.discover()

        assert len(devices) == 2
        assert devices[0].display_name == "Integrated Camera"
        assert devices[0].construct_kwargs == {"device_index": 0}

    def test_linux_without_dev_returns_empty(self, mock_cv2):
        from syncfield.adapters import uvc_webcam

        with patch.object(sys, "platform", "linux"), patch.object(
            uvc_webcam, "_discover_uvc_linux", return_value=[]
        ):
            assert uvc_webcam.UVCWebcamStream.discover() == []


class TestFallbackBranch:
    def test_unsupported_platform_returns_empty(self, mock_cv2):
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        with patch.object(sys, "platform", "win32"):
            assert UVCWebcamStream.discover() == []


class TestClassAttributes:
    def test_registry_hints_present(self, mock_cv2):
        from syncfield.adapters.uvc_webcam import UVCWebcamStream

        assert UVCWebcamStream._discovery_kind == "video"
        assert UVCWebcamStream._discovery_adapter_type == "uvc_webcam"
