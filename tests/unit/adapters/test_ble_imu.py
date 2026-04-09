"""Unit tests for BLEImuGenericStream using a mocked bleak module."""

from __future__ import annotations

import importlib
import struct
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from syncfield.clock import SessionClock
from syncfield.types import HealthEventKind, SyncPoint


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


@pytest.fixture
def mock_bleak(monkeypatch):
    fake = MagicMock()
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.start_notify = AsyncMock()
    client.stop_notify = AsyncMock()
    fake.BleakClient.return_value = client
    monkeypatch.setitem(sys.modules, "bleak", fake)
    sys.modules.pop("syncfield.adapters.ble_imu", None)
    importlib.import_module("syncfield.adapters.ble_imu")
    yield fake, client
    sys.modules.pop("syncfield.adapters.ble_imu", None)


def test_capabilities(mock_bleak):
    from syncfield.adapters.ble_imu import BLEImuGenericStream
    stream = BLEImuGenericStream(
        "imu", mac="AA:BB:CC:DD:EE:FF", characteristic_uuid="1234"
    )
    assert stream.kind == "sensor"
    assert stream.capabilities.provides_audio_track is False
    assert stream.capabilities.is_removable is True
    assert stream.capabilities.produces_file is False


def test_prepare_instantiates_client(mock_bleak):
    fake, _ = mock_bleak
    from syncfield.adapters.ble_imu import BLEImuGenericStream
    stream = BLEImuGenericStream(
        "imu", mac="00:11:22:33:44:55", characteristic_uuid="c"
    )
    stream.prepare()
    fake.BleakClient.assert_called_once_with("00:11:22:33:44:55")


def test_channel_name_length_must_match_format(mock_bleak):
    from syncfield.adapters.ble_imu import BLEImuGenericStream
    with pytest.raises(ValueError, match="channel_names"):
        BLEImuGenericStream(
            "imu",
            mac="m",
            characteristic_uuid="c",
            frame_format="<fff",  # 3 values
            channel_names=("a", "b"),  # 2 names — mismatch
        )


def test_start_stop_lifecycle_connects_and_disconnects(mock_bleak):
    _, client = mock_bleak
    from syncfield.adapters.ble_imu import BLEImuGenericStream
    stream = BLEImuGenericStream("imu", mac="m", characteristic_uuid="c")
    stream.prepare()
    stream.start(_clock())
    # Give the background async loop a moment to spin up
    time.sleep(0.1)
    report = stream.stop()
    assert report.status == "completed"
    assert client.connect.await_count >= 1
    assert client.start_notify.await_count >= 1


def test_notification_payload_is_decoded_and_emitted(mock_bleak):
    from syncfield.adapters.ble_imu import BLEImuGenericStream
    stream = BLEImuGenericStream(
        "imu",
        mac="m",
        characteristic_uuid="c",
        frame_format="<fff",
        channel_names=("ax", "ay", "az"),
    )
    received = []
    stream.on_sample(received.append)
    stream.prepare()

    # Feed a payload directly through the synchronous test hook — no need
    # to spin up the async loop for this test.
    payload = struct.pack("<fff", 1.0, 2.0, 3.0)
    stream._dispatch_notification_for_test(payload)

    assert len(received) == 1
    assert received[0].channels == {"ax": 1.0, "ay": 2.0, "az": 3.0}


def test_payload_decode_failure_emits_warning_health_event(mock_bleak):
    from syncfield.adapters.ble_imu import BLEImuGenericStream
    stream = BLEImuGenericStream(
        "imu", mac="m", characteristic_uuid="c", frame_format="<fff",
        channel_names=("a", "b", "c"),
    )
    received_health = []
    stream.on_health(received_health.append)
    stream._dispatch_notification_for_test(b"\x00\x01")  # too short

    assert len(received_health) == 1
    assert received_health[0].kind is HealthEventKind.WARNING


def test_bleak_missing_raises_clear_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "bleak", None)
    sys.modules.pop("syncfield.adapters.ble_imu", None)
    with pytest.raises(ImportError, match=r"syncfield\[ble\]"):
        importlib.import_module("syncfield.adapters.ble_imu")
