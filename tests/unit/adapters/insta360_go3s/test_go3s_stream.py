import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from syncfield.adapters.insta360_go3s import Go3SStream
from syncfield.adapters.insta360_go3s.aggregation.queue import AggregationQueue
from syncfield.adapters.insta360_go3s.aggregation.types import AggregationState
from syncfield.adapters.insta360_go3s.ble.camera import CaptureResult
from syncfield.types import StreamCapabilities


@pytest.fixture
def fake_ble(monkeypatch):
    """Replace Go3SBLECamera with an async-mock factory."""
    fake = AsyncMock()
    fake.connect = AsyncMock()
    fake.disconnect = AsyncMock()
    fake.set_video_mode = AsyncMock()
    fake.start_capture = AsyncMock(return_value=12345)  # fake host_ns
    fake.stop_capture = AsyncMock(
        return_value=CaptureResult(
            file_path="/DCIM/Camera01/VID_FAKE.mp4", ack_host_ns=23456
        )
    )

    def factory(address):
        return fake

    monkeypatch.setattr(
        "syncfield.adapters.insta360_go3s.stream.Go3SBLECamera", factory
    )
    return fake


@pytest.fixture
def fake_queue(monkeypatch):
    queue = MagicMock(spec=AggregationQueue)
    queue.enqueue = MagicMock()
    monkeypatch.setattr(
        "syncfield.adapters.insta360_go3s.stream._global_aggregation_queue",
        lambda: queue,
    )
    return queue


def test_capabilities_indicate_no_live_preview_and_produces_file(fake_ble, tmp_path):
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
    )
    caps: StreamCapabilities = s.capabilities
    assert caps.live_preview is False
    assert caps.produces_file is True
    assert caps.is_removable is True


def test_device_key_is_go3s_with_address(fake_ble, tmp_path):
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
    )
    assert s.device_key == ("go3s", "AA:BB:CC:DD:EE:FF")


def test_kind_is_video(fake_ble, tmp_path):
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
    )
    assert s.kind == "video"


def test_full_lifecycle_enqueues_aggregation(fake_ble, fake_queue, tmp_path):
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
        aggregation_policy="eager",  # default is on_demand; test the eager path
    )
    s.prepare()
    s.connect()
    # start_recording is a sync API; the implementation runs the async work internally.
    s.start_recording(session_clock=MagicMock())
    report = s.stop_recording()
    s.disconnect()

    assert report.status == "pending_aggregation"
    assert report.stream_id == "overhead"
    fake_queue.enqueue.assert_called_once()
    enq_job = fake_queue.enqueue.call_args.args[0]
    assert enq_job.cameras[0].stream_id == "overhead"
    assert enq_job.cameras[0].sd_path == "/DCIM/Camera01/VID_FAKE.mp4"
    assert enq_job.cameras[0].local_filename == "overhead.mp4"


def test_on_demand_policy_does_not_enqueue(fake_ble, fake_queue, tmp_path):
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
        aggregation_policy="on_demand",
    )
    s.prepare()
    s.connect()
    s.start_recording(session_clock=MagicMock())
    report = s.stop_recording()
    assert report.status == "pending_aggregation"
    assert not fake_queue.enqueue.called
    # An ID for manual aggregation later should still be exposed
    assert s.pending_aggregation_job is not None
    assert s.pending_aggregation_job.cameras[0].sd_path == "/DCIM/Camera01/VID_FAKE.mp4"


def test_ble_link_is_held_across_start_stop_no_rehandshake(monkeypatch, tmp_path):
    """Regression guard: connect() opens BLE once; start/stop reuse the link.

    Before persistent-BLE refactor, every command re-ran SYNC + CHECK_AUTH
    (~4–7 s on macOS). The fix is to hold the connection open across the
    recording window. This test fails if Go3SBLECamera is instantiated more
    than once per session.
    """
    instantiations = []

    class RecordingCam:
        def __init__(self, address):
            instantiations.append(address)
            self.address = address
            self.is_connected = False
            self.ble_name = "GO 3S TEST"
        async def connect(self, sync_timeout=2.0, auth_timeout=1.0, discovery_timeout=5.0):
            self.is_connected = True
        async def start_capture(self):
            return 111
        async def stop_capture(self):
            return CaptureResult(file_path="/DCIM/Camera01/VID.mp4", ack_host_ns=222)
        async def disconnect(self):
            self.is_connected = False

    monkeypatch.setattr(
        "syncfield.adapters.insta360_go3s.stream.Go3SBLECamera", RecordingCam
    )
    monkeypatch.setattr(
        "syncfield.adapters.insta360_go3s.stream._global_aggregation_queue",
        lambda: MagicMock(spec=AggregationQueue),
    )

    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
    )
    s.prepare()
    s.connect()
    s.start_recording(session_clock=MagicMock())
    report = s.stop_recording()
    s.disconnect()

    assert report.status == "pending_aggregation"
    assert len(instantiations) == 1, (
        f"Expected exactly one Go3SBLECamera instantiation; got "
        f"{len(instantiations)}. Each extra instantiation means another "
        f"SYNC + CHECK_AUTH handshake (~4–7 s latency)."
    )


def test_stop_recording_returns_failed_when_ble_returns_empty_filepath(fake_ble, fake_queue, tmp_path):
    """If BLE STOP doesn't echo a /DCIM/... path, return status='failed'.

    We do NOT raise here — raising would interrupt the orchestrator's stop
    fan-out across other streams. Instead, surface a FinalizationReport with
    status='failed' and a clear error message so the viewer and caller can
    see that the camera may still be recording.
    """
    fake_ble.stop_capture.return_value = CaptureResult(file_path="", ack_host_ns=0)
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
    )
    s.prepare()
    s.connect()
    s.start_recording(session_clock=MagicMock())
    report = s.stop_recording()
    assert report.status == "failed"
    assert "did not return a file path" in (report.error or "")
    # No aggregation should have been enqueued for a failed stop
    assert not fake_queue.enqueue.called
    assert s.pending_aggregation_job is None


def test_recovery_scan_picks_up_pending_aggregation(tmp_path, monkeypatch):
    """A leftover aggregation.json from a prior run is re-enqueued at startup."""
    import json
    import time as _time
    from syncfield.adapters.insta360_go3s.aggregation.types import (
        AggregationCameraSpec, AggregationJob, AggregationState,
    )

    # Reset the singleton so the test triggers fresh init.
    import syncfield.adapters.insta360_go3s.stream as _stream_mod
    monkeypatch.setattr(_stream_mod, "_QUEUE", None)
    monkeypatch.setattr(_stream_mod, "_QUEUE_LOOP", None)
    monkeypatch.setattr(_stream_mod, "_QUEUE_THREAD", None)
    monkeypatch.setenv("SYNCFIELD_GO3S_RECOVERY_ROOT", str(tmp_path))

    # Plant a pending aggregation manifest.
    ep_dir = tmp_path / "ep_recover"
    ep_dir.mkdir()
    job = AggregationJob(
        job_id="agg_recover",
        episode_id="ep_recover",
        episode_dir=ep_dir,
        cameras=[AggregationCameraSpec(
            stream_id="overhead",
            ble_address="AA:BB",
            wifi_ssid="Go3S-X.OSC",
            wifi_password="88888888",
            sd_path="/DCIM/Camera01/X.mp4",
            local_filename="overhead.mp4",
            size_bytes=0,
        )],
        state=AggregationState.PENDING,
    )
    job.write_manifest()

    # Stub the production downloader so it succeeds without real WiFi.
    from syncfield.adapters.insta360_go3s.aggregation.queue import (
        AggregationDownloader,
    )

    class NoOpDownloader(AggregationDownloader):
        async def run(self, camera, target_dir, on_chunk, on_stage=None):
            pass  # instant "success"

    monkeypatch.setattr(
        _stream_mod,
        "Go3SAggregationDownloader",
        lambda *args, **kwargs: NoOpDownloader(),
    )
    # Stub wifi_switcher_for_platform so init doesn't probe real networks.
    monkeypatch.setattr(
        _stream_mod,
        "wifi_switcher_for_platform",
        lambda: MagicMock(),
    )

    from syncfield.adapters.insta360_go3s.stream import _global_aggregation_queue
    _global_aggregation_queue()

    # Give the worker a moment to drain the recovered job.
    for _ in range(40):
        manifest = json.loads((ep_dir / "aggregation.json").read_text())
        if manifest["state"] == "completed":
            break
        _time.sleep(0.05)

    assert manifest["state"] == "completed", (
        f"recovered job not processed: state={manifest['state']}"
    )

    # Teardown: reset singleton so later tests start fresh if they need it.
    monkeypatch.setattr(_stream_mod, "_QUEUE", None)
    monkeypatch.setattr(_stream_mod, "_QUEUE_LOOP", None)
    monkeypatch.setattr(_stream_mod, "_QUEUE_THREAD", None)
