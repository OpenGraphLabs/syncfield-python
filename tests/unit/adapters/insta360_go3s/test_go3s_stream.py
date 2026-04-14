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


def test_stop_recording_raises_when_ble_returns_empty_filepath(fake_ble, fake_queue, tmp_path):
    """If BLE STOP doesn't echo a /DCIM/... path, surface a clear error."""
    fake_ble.stop_capture.return_value = CaptureResult(file_path="", ack_host_ns=0)
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
    )
    s.prepare()
    s.connect()
    s.start_recording(session_clock=MagicMock())
    with pytest.raises(RuntimeError, match="did not return a file path"):
        s.stop_recording()
