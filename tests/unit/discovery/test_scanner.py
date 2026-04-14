"""Unit tests for scan() and scan_and_add() — the discovery coordinator.

The tests use stub adapter classes registered into the discovery
registry to drive the scan path without any real hardware. Each test
starts with a clean registry (via ``clear_registry``) so no cross-test
leakage is possible.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, List

import pytest

import syncfield as sf
from syncfield.discovery import (
    DiscoveredDevice,
    DiscoveryReport,
    clear_registry,
    clear_scan_cache,
    iter_discoverers,
    register_discoverer,
    scan,
    scan_and_add,
    unregister_discoverer,
)
from syncfield.stream import StreamBase
from syncfield.testing import FakeStream
from syncfield.types import FinalizationReport, StreamCapabilities


# ---------------------------------------------------------------------------
# Stub adapters — classmethod discover() + required class attributes
# ---------------------------------------------------------------------------


class _StubStreamBase(StreamBase):
    """Minimal Stream subclass for scanner tests — no-op lifecycle, records
    constructor kwargs so tests can assert on what ``scan_and_add`` passed."""

    def __init__(self, *, id: str, **kwargs: Any) -> None:
        super().__init__(
            id=id,
            kind=self._discovery_kind,  # type: ignore[arg-type]
            capabilities=StreamCapabilities(),
        )
        # Keep a full record of construction kwargs (including id itself)
        # so tests can assert on what scan_and_add forwarded.
        self.kwargs: dict[str, Any] = {"id": id, **kwargs}

    def prepare(self) -> None:  # pragma: no cover — tests never start the session
        pass

    def start(self, session_clock) -> None:  # pragma: no cover
        pass

    def stop(self) -> FinalizationReport:  # pragma: no cover
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=0,
            file_path=None,
            first_sample_at_ns=None,
            last_sample_at_ns=None,
            health_events=[],
            error=None,
        )


class _StubVideoAdapter(_StubStreamBase):
    """Stand-in for a camera adapter. Returns a fixed list of 'cameras'."""

    _discovery_kind = "video"
    _discovery_adapter_type = "stub_video"

    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> List[DiscoveredDevice]:
        return [
            DiscoveredDevice(
                adapter_type="stub_video",
                adapter_cls=cls,
                kind="video",
                display_name="Stub Camera A",
                description="fake",
                device_id="a",
                construct_kwargs={"index": 0},
                accepts_output_dir=True,
            ),
            DiscoveredDevice(
                adapter_type="stub_video",
                adapter_cls=cls,
                kind="video",
                display_name="Stub Camera B",
                description="fake",
                device_id="b",
                construct_kwargs={"index": 1},
                accepts_output_dir=True,
            ),
        ]


class _StubSensorAdapter(_StubStreamBase):
    _discovery_kind = "sensor"
    _discovery_adapter_type = "stub_sensor"

    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> List[DiscoveredDevice]:
        return [
            DiscoveredDevice(
                adapter_type="stub_sensor",
                adapter_cls=cls,
                kind="sensor",
                display_name="Stub IMU",
                description="fake",
                device_id="imu",
                construct_kwargs={"mac": "AA:BB:CC"},
                accepts_output_dir=False,
            ),
        ]


class _SlowAdapter(_StubStreamBase):
    """Sleeps past the budget — should land in timed_out."""

    _discovery_kind = "sensor"
    _discovery_adapter_type = "slow"

    # Sleep just long enough to exceed the test's deadline; the executor's
    # shutdown waits on this thread, so keep it small.
    SLEEP_S = 0.15

    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> List[DiscoveredDevice]:
        time.sleep(cls.SLEEP_S)
        return []


class _FailingAdapter(_StubStreamBase):
    """Raises on discover() — should populate errors."""

    _discovery_kind = "sensor"
    _discovery_adapter_type = "failing"

    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> List[DiscoveredDevice]:
        raise RuntimeError("synthetic failure")


class _BrokenAdapterNeedsUuid(_StubStreamBase):
    """Stand-in for a BLE IMU that requires manual characteristic_uuid."""

    _discovery_kind = "sensor"
    _discovery_adapter_type = "broken_ble"

    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> List[DiscoveredDevice]:
        return [
            DiscoveredDevice(
                adapter_type="broken_ble",
                adapter_cls=cls,
                kind="sensor",
                display_name="Mystery IMU",
                description="ble:XX:YY",
                device_id="xxyy",
                construct_kwargs={"mac": "XX:YY"},
                accepts_output_dir=False,
                warnings=("characteristic_uuid required — add manually",),
            ),
        ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry_and_cache():
    """Every test starts with an empty registry and cache."""
    clear_registry()
    clear_scan_cache()
    yield
    clear_registry()
    clear_scan_cache()


@pytest.fixture
def tmp_session(tmp_path):
    return sf.SessionOrchestrator(
        host_id="test",
        output_dir=tmp_path,
        sync_tone=sf.SyncToneConfig.silent(),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_iter(self):
        register_discoverer(_StubVideoAdapter)
        assert _StubVideoAdapter in iter_discoverers()

    def test_register_idempotent(self):
        register_discoverer(_StubVideoAdapter)
        register_discoverer(_StubVideoAdapter)
        assert len(iter_discoverers()) == 1

    def test_unregister(self):
        register_discoverer(_StubVideoAdapter)
        assert unregister_discoverer(_StubVideoAdapter) is True
        assert _StubVideoAdapter not in iter_discoverers()

    def test_unregister_missing_returns_false(self):
        assert unregister_discoverer(_StubVideoAdapter) is False

    def test_register_requires_discover_method(self):
        class Broken:
            _discovery_kind = "video"

        with pytest.raises(TypeError, match="discover"):
            register_discoverer(Broken)

    def test_register_requires_kind_attribute(self):
        class NoKind:
            @classmethod
            def discover(cls, *, timeout=5.0):
                return []

        with pytest.raises(TypeError, match="_discovery_kind"):
            register_discoverer(NoKind)


# ---------------------------------------------------------------------------
# scan()
# ---------------------------------------------------------------------------


class TestScan:
    def test_empty_registry_returns_empty_report(self):
        report = scan()
        assert report.devices == ()
        assert report.errors == {}
        assert report.timed_out == ()

    def test_single_adapter(self):
        register_discoverer(_StubVideoAdapter)
        report = scan(use_cache=False)
        assert len(report.devices) == 2
        assert all(d.adapter_type == "stub_video" for d in report.devices)

    def test_multiple_adapters_aggregate(self):
        register_discoverer(_StubVideoAdapter)
        register_discoverer(_StubSensorAdapter)
        report = scan(use_cache=False)
        assert len(report.devices) == 3

    def test_kinds_filter(self):
        register_discoverer(_StubVideoAdapter)
        register_discoverer(_StubSensorAdapter)
        report = scan(kinds=["video"], use_cache=False)
        assert len(report.devices) == 2
        assert all(d.kind == "video" for d in report.devices)

    def test_failing_adapter_becomes_error_entry(self):
        register_discoverer(_StubVideoAdapter)
        register_discoverer(_FailingAdapter)
        report = scan(use_cache=False)
        assert len(report.devices) == 2  # the good one still works
        assert "failing" in report.errors
        assert "synthetic failure" in report.errors["failing"]

    def test_slow_adapter_times_out(self):
        register_discoverer(_StubVideoAdapter)
        register_discoverer(_SlowAdapter)
        # Deadline is well below _SlowAdapter.SLEEP_S so it must time out.
        report = scan(timeout=0.05, use_cache=False)
        # Good adapter should still have landed
        assert len(report.devices) == 2
        assert "slow" in report.timed_out

    def test_cache_hit_returns_same_object(self):
        register_discoverer(_StubVideoAdapter)
        first = scan()
        second = scan()
        # Same cache entry — identity check
        assert first is second

    def test_cache_miss_on_different_filter(self):
        register_discoverer(_StubVideoAdapter)
        register_discoverer(_StubSensorAdapter)
        all_report = scan()
        video_report = scan(kinds=["video"])
        assert all_report is not video_report
        assert len(all_report.devices) == 3
        assert len(video_report.devices) == 2

    def test_use_cache_false_forces_fresh_scan(self):
        register_discoverer(_StubVideoAdapter)
        first = scan()
        second = scan(use_cache=False)
        assert first is not second
        # But the content matches
        assert len(first.devices) == len(second.devices)


# ---------------------------------------------------------------------------
# scan_and_add()
# ---------------------------------------------------------------------------


class TestScanAndAdd:
    def test_registers_all_found_devices(self, tmp_session):
        register_discoverer(_StubVideoAdapter)
        register_discoverer(_StubSensorAdapter)

        added = scan_and_add(tmp_session)

        assert len(added) == 3
        assert len(tmp_session._streams) == 3  # noqa: SLF001
        assert set(tmp_session._streams.keys()) == {  # noqa: SLF001
            "stub_camera_a",
            "stub_camera_b",
            "stub_imu",
        }

    def test_skips_devices_with_warnings(self, tmp_session):
        register_discoverer(_BrokenAdapterNeedsUuid)
        added = scan_and_add(tmp_session)
        assert added == []
        assert tmp_session._streams == {}  # noqa: SLF001

    def test_respects_kind_filter(self, tmp_session):
        register_discoverer(_StubVideoAdapter)
        register_discoverer(_StubSensorAdapter)
        added = scan_and_add(tmp_session, kinds=["video"])
        assert len(added) == 2
        assert all(d.kind == "video" for d in added)

    def test_id_prefix_applied(self, tmp_session):
        register_discoverer(_StubVideoAdapter)
        scan_and_add(tmp_session, id_prefix="lab")
        assert all(
            sid.startswith("lab_")
            for sid in tmp_session._streams.keys()  # noqa: SLF001
        )

    def test_skip_existing_stream_id(self, tmp_session):
        tmp_session.add(FakeStream("stub_camera_a"))
        register_discoverer(_StubVideoAdapter)

        added = scan_and_add(tmp_session)
        # "stub_camera_a" was pre-existing → a collision-avoiding id
        # (stub_camera_a_0 or similar) should be used for the new one
        assert len(added) == 2
        streams = tmp_session._streams  # noqa: SLF001
        assert "stub_camera_a" in streams  # pre-existing FakeStream
        # New one must have a distinct id
        new_ids = set(streams.keys()) - {"stub_camera_a"}
        assert any(i.startswith("stub_camera_a") for i in new_ids)

    def test_refuses_non_idle_session(self, tmp_path):
        session = sf.SessionOrchestrator(
            host_id="test",
            output_dir=tmp_path,
            sync_tone=sf.SyncToneConfig.silent(),
        )
        session.add(FakeStream("x"))
        session.start()
        try:
            register_discoverer(_StubVideoAdapter)
            with pytest.raises(RuntimeError, match="IDLE"):
                scan_and_add(session)
        finally:
            session.stop()

    def test_output_dir_injected_for_video_adapters(self, tmp_session):
        register_discoverer(_StubVideoAdapter)
        scan_and_add(tmp_session)
        # _StubVideoAdapter records kwargs; both instances should have
        # received an output_dir equal to the session's.
        for stream in tmp_session._streams.values():  # noqa: SLF001
            assert "output_dir" in stream.kwargs
            assert stream.kwargs["output_dir"] == tmp_session.output_dir

    def test_output_dir_not_injected_for_sensor_adapters(self, tmp_session):
        register_discoverer(_StubSensorAdapter)
        scan_and_add(tmp_session)
        sensor_stream = next(iter(tmp_session._streams.values()))  # noqa: SLF001
        assert "output_dir" not in sensor_stream.kwargs
