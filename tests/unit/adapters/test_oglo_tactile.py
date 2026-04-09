"""Unit tests for OgloTactileStream.

Exercises the synchronous decode path directly via
``_dispatch_notification_for_test`` so the whole test module runs without
bleak hardware or an asyncio loop. The import-guard test uses a patched
``sys.modules`` entry to simulate bleak being absent.
"""

from __future__ import annotations

import importlib
import json
import struct
import sys
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from syncfield.clock import SessionClock
from syncfield.types import HealthEventKind, SampleEvent, SyncPoint


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


def _build_packet(count: int, timestamp_us: int, samples: list[tuple[int, ...]]) -> bytes:
    """Build one OGLO notification payload (``<HI`` header + count × ``<5H``)."""
    assert len(samples) == count
    header = struct.pack("<HI", count, timestamp_us)
    body = b"".join(struct.pack("<5H", *sample) for sample in samples)
    return header + body


@pytest.fixture
def mock_bleak(monkeypatch):
    """Install a fake ``bleak`` module so the adapter imports cleanly."""
    fake = MagicMock()
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.start_notify = AsyncMock()
    client.stop_notify = AsyncMock()
    fake.BleakClient.return_value = client
    fake.BleakScanner = MagicMock()
    fake.BleakScanner.discover = AsyncMock(return_value=[])
    monkeypatch.setitem(sys.modules, "bleak", fake)
    sys.modules.pop("syncfield.adapters.oglo_tactile", None)
    importlib.import_module("syncfield.adapters.oglo_tactile")
    yield fake, client
    sys.modules.pop("syncfield.adapters.oglo_tactile", None)


class TestConstruction:
    def test_capabilities(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        stream = OgloTactileStream("tactile_right", address="AA:BB:CC:DD:EE:FF")
        assert stream.kind == "sensor"
        assert stream.capabilities.provides_audio_track is False
        assert stream.capabilities.is_removable is True
        assert stream.capabilities.produces_file is False
        assert stream.capabilities.supports_precise_timestamps is True

    def test_hand_property(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        stream = OgloTactileStream(
            "tactile_right", address="AA:BB:CC:DD:EE:FF", hand="right"
        )
        assert stream.hand == "right"

    def test_default_scan_filter_is_oglo(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        stream = OgloTactileStream("t", address="00:11:22:33:44:55")
        assert stream._ble_name == "oglo"

    def test_uuids_match_egonaut_constants(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import (
            CONFIG_CHAR_UUID,
            NOTIFY_CHAR_UUID,
            SERVICE_UUID,
        )

        assert SERVICE_UUID == "4652535f-424c-4500-0000-000000000001"
        assert NOTIFY_CHAR_UUID == "4652535f-424c-4500-0001-000000000001"
        assert CONFIG_CHAR_UUID == "4652535f-424c-4500-0002-000000000001"

    def test_canonical_finger_order(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import FINGER_NAMES

        assert FINGER_NAMES == ("thumb", "index", "middle", "ring", "pinky")


class TestPayloadDecoding:
    def test_decodes_full_batch(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        stream = OgloTactileStream("tactile_right", address="m", hand="right")
        received: List[SampleEvent] = []
        stream.on_sample(received.append)

        samples = [
            (100, 200, 300, 400, 500),
            (110, 210, 310, 410, 510),
            (120, 220, 320, 420, 520),
        ]
        packet = _build_packet(count=3, timestamp_us=12_345_000, samples=samples)
        stream._dispatch_notification_for_test(packet)

        assert len(received) == 3
        # First sample has the exact batch base timestamp in device ns.
        first = received[0].channels
        assert first is not None
        assert first["thumb"] == 100
        assert first["index"] == 200
        assert first["middle"] == 300
        assert first["ring"] == 400
        assert first["pinky"] == 500
        assert first["device_timestamp_ns"] == 12_345_000 * 1000

        # Frame numbers increment across the batch
        assert [ev.frame_number for ev in received] == [0, 1, 2]

    def test_device_timestamp_interpolated_uniformly(self, mock_bleak):
        """Consecutive samples in one batch should be 10 ms apart in device_ns."""
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        stream = OgloTactileStream("tactile_right", address="m")
        received: List[SampleEvent] = []
        stream.on_sample(received.append)

        samples = [(i, i, i, i, i) for i in range(10)]
        packet = _build_packet(count=10, timestamp_us=1_000_000, samples=samples)
        stream._dispatch_notification_for_test(packet)

        assert len(received) == 10
        device_ns = [ev.channels["device_timestamp_ns"] for ev in received]  # type: ignore[index]
        deltas = [device_ns[i + 1] - device_ns[i] for i in range(9)]
        # 10 ms = 10_000 µs = 10_000_000 ns, exactly and consistently.
        assert all(d == 10_000_000 for d in deltas)

    def test_frame_count_accumulates_across_batches(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        stream = OgloTactileStream("tactile_right", address="m")

        for batch_index in range(3):
            samples = [(1, 2, 3, 4, 5)] * 10
            packet = _build_packet(
                count=10,
                timestamp_us=1_000_000 + batch_index * 100_000,
                samples=samples,
            )
            stream._dispatch_notification_for_test(packet)

        assert stream._frame_count == 30

    def test_short_packet_becomes_warning(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        stream = OgloTactileStream("tactile_right", address="m")
        received_health: List = []
        stream.on_health(received_health.append)

        stream._dispatch_notification_for_test(b"\x01\x00")  # 2 bytes

        assert len(received_health) == 1
        assert received_health[0].kind is HealthEventKind.WARNING
        assert stream._frame_count == 0

    def test_truncated_body_becomes_warning(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        stream = OgloTactileStream("tactile_right", address="m")
        received_health: List = []
        stream.on_health(received_health.append)

        # Header claims 10 samples but body only carries 1.
        bad_packet = struct.pack("<HI", 10, 1) + struct.pack("<5H", 1, 2, 3, 4, 5)
        stream._dispatch_notification_for_test(bad_packet)

        assert len(received_health) == 1
        assert "truncated" in (received_health[0].detail or "").lower()
        assert stream._frame_count == 0

    def test_finalization_report_carries_counts(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        stream = OgloTactileStream("tactile_right", address="m", hand="right")

        packet = _build_packet(
            count=5,
            timestamp_us=0,
            samples=[(1, 2, 3, 4, 5)] * 5,
        )
        stream._dispatch_notification_for_test(packet)
        report = stream.stop()

        assert report.stream_id == "tactile_right"
        assert report.frame_count == 5
        assert report.status == "completed"
        assert report.first_sample_at_ns is not None
        assert report.last_sample_at_ns is not None


class TestConstructionErrors:
    def test_rejects_both_address_and_name_missing(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        with pytest.raises(ValueError, match="address.*ble_name"):
            OgloTactileStream("tactile_right", address=None, ble_name="")


class TestImportGuard:
    def test_missing_bleak_raises_clear_install_hint(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "bleak", None)
        sys.modules.pop("syncfield.adapters.oglo_tactile", None)
        with pytest.raises(ImportError, match=r"syncfield\[ble\]"):
            importlib.import_module("syncfield.adapters.oglo_tactile")


class TestLazyExport:
    def test_appears_in_syncfield_adapters(self, mock_bleak):
        import syncfield.adapters as adapters

        # Force re-import of the adapters package so the lazy try/except
        # sees our mocked bleak.
        importlib.reload(adapters)
        assert "OgloTactileStream" in adapters.__all__
        assert hasattr(adapters, "OgloTactileStream")
