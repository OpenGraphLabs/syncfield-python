"""Unit tests for the discovery data model (DiscoveredDevice, DiscoveryReport)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from syncfield.discovery.types import DiscoveredDevice, DiscoveryReport


class _StubAdapter:
    """Stand-in for a Stream class in tests — just records constructor kwargs."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _make_device(**overrides: Any) -> DiscoveredDevice:
    defaults: dict[str, Any] = {
        "adapter_type": "stub",
        "adapter_cls": _StubAdapter,
        "kind": "video",
        "display_name": "Stub Camera",
        "description": "1920×1080 · stub",
        "device_id": "stub:0",
        "construct_kwargs": {"device_index": 0},
        "accepts_output_dir": True,
    }
    defaults.update(overrides)
    return DiscoveredDevice(**defaults)


class TestDiscoveredDevice:
    def test_frozen(self):
        device = _make_device()
        with pytest.raises(dataclasses.FrozenInstanceError):
            device.display_name = "other"  # type: ignore[misc]

    def test_construct_merges_kwargs(self):
        device = _make_device(construct_kwargs={"device_index": 2})
        stream = device.construct(id="cam_main", output_dir="/tmp/data")
        assert isinstance(stream, _StubAdapter)
        assert stream.kwargs == {
            "device_index": 2,
            "id": "cam_main",
            "output_dir": "/tmp/data",
        }

    def test_construct_caller_overrides_discovered(self):
        """Caller kwargs win on conflict (overriding discovery-set defaults)."""
        device = _make_device(construct_kwargs={"fps": 30})
        stream = device.construct(id="cam", fps=60)
        assert stream.kwargs["fps"] == 60

    def test_construct_requires_id(self):
        device = _make_device()
        with pytest.raises(TypeError, match="'id'"):
            device.construct(output_dir="/tmp")

    def test_defaults(self):
        device = DiscoveredDevice(
            adapter_type="x",
            adapter_cls=_StubAdapter,
            kind="video",
            display_name="X",
            description="",
            device_id="0",
        )
        assert device.construct_kwargs == {}
        assert device.accepts_output_dir is False
        assert device.in_use is False
        assert device.warnings == ()


class TestDiscoveryReport:
    def test_by_kind(self):
        cam = _make_device(kind="video", display_name="Cam")
        imu = _make_device(kind="sensor", display_name="IMU")
        report = DiscoveryReport(devices=(cam, imu))
        assert report.by_kind("video") == (cam,)
        assert report.by_kind("sensor") == (imu,)
        assert report.by_kind("audio") == ()

    def test_by_adapter_type(self):
        cam = _make_device(adapter_type="uvc_webcam")
        oak = _make_device(adapter_type="oak_camera")
        report = DiscoveryReport(devices=(cam, oak))
        assert report.by_adapter_type("uvc_webcam") == (cam,)
        assert report.by_adapter_type("oak_camera") == (oak,)

    def test_is_success(self):
        assert DiscoveryReport(devices=()).is_success is True
        assert DiscoveryReport(
            devices=(), errors={"ble": "oops"}
        ).is_success is False
        assert DiscoveryReport(
            devices=(), timed_out=("ble",)
        ).is_success is False

    def test_summary(self):
        device = _make_device()
        report = DiscoveryReport(devices=(device,), duration_s=1.234)
        assert "1 devices" in report.summary()
        assert "1.2s" in report.summary()

    def test_summary_with_errors_and_timeouts(self):
        report = DiscoveryReport(
            devices=(),
            errors={"ble": "no adapter"},
            duration_s=2.0,
            timed_out=("oak_camera",),
        )
        summary = report.summary()
        assert "error" in summary
        assert "timed out" in summary
