"""Unit tests for the profile-driven BLEImuGenericStream.

Tests are split into three layers:

1. :class:`BLEImuProfile` validation — catches bad presets at
   construction time, not at runtime.
2. Decode path — synchronous, feeds bytes through
   ``_dispatch_notification_for_test``; no asyncio / bleak required.
3. Lifecycle + config writes — exercises the async path using a mocked
   ``bleak`` module so the tests stay hermetic.
"""

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


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_bleak(monkeypatch):
    """Install a fake ``bleak`` module so ``ble_imu`` imports cleanly."""
    fake = MagicMock()
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.start_notify = AsyncMock()
    client.stop_notify = AsyncMock()
    client.write_gatt_char = AsyncMock()
    fake.BleakClient.return_value = client
    monkeypatch.setitem(sys.modules, "bleak", fake)
    sys.modules.pop("syncfield.adapters.ble_imu", None)
    importlib.import_module("syncfield.adapters.ble_imu")
    yield fake, client
    sys.modules.pop("syncfield.adapters.ble_imu", None)


def _simple_profile(mock_bleak):
    """Return a tiny BLEImuProfile with a 2B header + 3 int16 channels."""
    from syncfield.adapters.ble_imu import BLEImuProfile, ChannelSpec
    return BLEImuProfile(
        notify_uuid="cafe",
        struct_format="<hhh",
        channels=(
            ChannelSpec("x", scale=0.5),
            ChannelSpec("y", scale=0.5),
            ChannelSpec("z", scale=0.5, offset=1.0),
        ),
        frame_header=b"\xAA\xBB",
    )


# ============================================================================
# BLEImuProfile validation
# ============================================================================


class TestProfileValidation:
    def test_channel_count_must_match_struct_format(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuProfile, ChannelSpec
        with pytest.raises(ValueError, match="channels declared"):
            BLEImuProfile(
                notify_uuid="c",
                struct_format="<fff",   # 3 values
                channels=(ChannelSpec("a"), ChannelSpec("b")),  # 2 names
            )

    def test_samples_per_frame_must_be_at_least_one(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuProfile, ChannelSpec
        with pytest.raises(ValueError, match="samples_per_frame"):
            BLEImuProfile(
                notify_uuid="c", struct_format="<h",
                channels=(ChannelSpec("a"),),
                samples_per_frame=0,
            )

    def test_batching_requires_positive_sample_period(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuProfile, ChannelSpec
        with pytest.raises(ValueError, match="sample_period_us"):
            BLEImuProfile(
                notify_uuid="c", struct_format="<h",
                channels=(ChannelSpec("a"),),
                samples_per_frame=5,        # batched
                sample_period_us=0,         # but no spacing declared
            )

    def test_sample_stride_accounts_for_header_and_body(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuProfile, ChannelSpec
        profile = BLEImuProfile(
            notify_uuid="c",
            struct_format="<h",             # 2-byte body per sample
            channels=(ChannelSpec("a"),),
            frame_header=b"\x55\x61",       # 2-byte per-sample prefix
        )
        assert profile.sample_stride == 2 + 2

    def test_samples_per_frame_defaults_to_auto(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuProfile, ChannelSpec
        profile = BLEImuProfile(
            notify_uuid="c",
            struct_format="<h",
            channels=(ChannelSpec("a"),),
        )
        assert profile.samples_per_frame is None


# ============================================================================
# Stream construction
# ============================================================================


class TestStreamConstruction:
    def test_capabilities(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuGenericStream
        stream = BLEImuGenericStream(
            "imu", profile=_simple_profile(mock_bleak), address="AA:BB",
        )
        assert stream.kind == "sensor"
        assert stream.capabilities.provides_audio_track is False
        assert stream.capabilities.is_removable is True
        assert stream.capabilities.produces_file is False

    def test_requires_address_or_name(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuGenericStream
        with pytest.raises(ValueError, match="address.*ble_name"):
            BLEImuGenericStream("imu", profile=_simple_profile(mock_bleak))

    def test_prepare_with_explicit_address_skips_scan(self, mock_bleak):
        fake, _ = mock_bleak
        from syncfield.adapters.ble_imu import BLEImuGenericStream
        stream = BLEImuGenericStream(
            "imu", profile=_simple_profile(mock_bleak), address="00:11",
        )
        stream.prepare()
        assert stream._device == "00:11"
        fake.BleakScanner.discover.assert_not_called()

    def test_prepare_via_name_scan_persists_only_the_address_string(
        self, monkeypatch, mock_bleak,
    ):
        """Regression guard: ``prepare()`` must not stash the scanned
        ``BLEDevice`` itself. The device object carries affinity to the
        scanner's event loop; reusing it from the background connect
        loop raises "Future attached to a different loop" on bleak 3.x.
        We keep only the raw address string, which is loop-agnostic."""
        from syncfield.adapters.ble_imu import BLEImuGenericStream

        class FakeBLEDevice:
            address = "AA:BB:CC:DD:EE:FF"
            name = "WT901BLE-FAKE"

        async def fake_scan(self):
            return FakeBLEDevice()

        monkeypatch.setattr(
            BLEImuGenericStream, "_scan_for_device", fake_scan
        )
        stream = BLEImuGenericStream(
            "imu", profile=_simple_profile(mock_bleak), ble_name="WT",
        )
        stream.prepare()

        assert isinstance(stream._device, str)
        assert stream._device == "AA:BB:CC:DD:EE:FF"


# ============================================================================
# Decode path
# ============================================================================


class TestDecode:
    def test_valid_frame_emits_scaled_sample(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuGenericStream
        stream = BLEImuGenericStream(
            "imu", profile=_simple_profile(mock_bleak), address="x",
        )
        received = []
        stream.on_sample(received.append)

        # header + 3 int16 LE = 2 + 6 = 8 bytes
        payload = b"\xAA\xBB" + struct.pack("<hhh", 100, 200, 300)
        stream._dispatch_notification_for_test(payload)

        assert len(received) == 1
        # scale=0.5 for all; offset=+1 on z
        assert received[0].channels == {
            "x": 50.0,
            "y": 100.0,
            "z": 151.0,
        }

    def test_non_stride_multiple_length_downgrades_to_warning(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuGenericStream
        stream = BLEImuGenericStream(
            "imu", profile=_simple_profile(mock_bleak), address="x",
        )
        samples, healths = [], []
        stream.on_sample(samples.append)
        stream.on_health(healths.append)

        # stride is 8 (2-byte header + 6-byte body); 2 bytes isn't a
        # positive multiple, so the payload is rejected.
        stream._dispatch_notification_for_test(b"\x00\x01")

        assert samples == []
        assert len(healths) == 1
        assert healths[0].kind is HealthEventKind.WARNING
        assert "stride" in healths[0].detail

    def test_header_mismatch_downgrades_to_warning(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuGenericStream
        stream = BLEImuGenericStream(
            "imu", profile=_simple_profile(mock_bleak), address="x",
        )
        samples, healths = [], []
        stream.on_sample(samples.append)
        stream.on_health(healths.append)

        # Right length (8 bytes) but wrong magic prefix.
        payload = b"\x55\x61" + struct.pack("<hhh", 0, 0, 0)
        stream._dispatch_notification_for_test(payload)

        assert samples == []
        assert len(healths) == 1
        assert healths[0].kind is HealthEventKind.WARNING
        assert "frame_header mismatch" in healths[0].detail

    def test_empty_header_means_no_header_check(self, mock_bleak):
        from syncfield.adapters.ble_imu import (
            BLEImuGenericStream,
            BLEImuProfile,
            ChannelSpec,
        )
        profile = BLEImuProfile(
            notify_uuid="c",
            struct_format="<h",
            channels=(ChannelSpec("v"),),
            # frame_header defaults to b"" → no check
        )
        stream = BLEImuGenericStream("imu", profile=profile, address="x")
        received = []
        stream.on_sample(received.append)

        stream._dispatch_notification_for_test(struct.pack("<h", 42))
        assert received[0].channels == {"v": 42}

    def test_fixed_samples_per_frame_emits_batch_with_interpolated_timestamps(
        self, mock_bleak
    ):
        from syncfield.adapters.ble_imu import (
            BLEImuGenericStream,
            BLEImuProfile,
            ChannelSpec,
        )
        profile = BLEImuProfile(
            notify_uuid="c",
            struct_format="<h",
            channels=(ChannelSpec("v"),),
            samples_per_frame=4,       # fixed batch size
            sample_period_us=10_000,   # 100 Hz effective → 10 ms apart
        )
        stream = BLEImuGenericStream("imu", profile=profile, address="x")
        received = []
        stream.on_sample(received.append)

        payload = struct.pack("<hhhh", 10, 20, 30, 40)
        stream._dispatch_notification_for_test(payload)

        assert [s.channels["v"] for s in received] == [10, 20, 30, 40]
        # Timestamps step by 10 ms (= 10_000_000 ns) per sample.
        deltas = [
            received[i + 1].capture_ns - received[i].capture_ns
            for i in range(len(received) - 1)
        ]
        assert deltas == [10_000_000, 10_000_000, 10_000_000]

    def test_auto_samples_per_frame_covers_bundling_factor_change(
        self, mock_bleak
    ):
        """Same profile must decode both a single-sample notification
        (sensor at low rate, one sample per BLE notify) and a bundled
        multi-sample notification (sensor at high rate, firmware packs
        N samples per notify). This is the exact WitMotion WT901BLE
        behavior across the 10 Hz → 200 Hz jump."""
        from syncfield.adapters.ble_imu import (
            BLEImuGenericStream,
            BLEImuProfile,
            ChannelSpec,
        )
        profile = BLEImuProfile(
            notify_uuid="c",
            struct_format="<h",
            channels=(ChannelSpec("v"),),
            frame_header=b"\x55\x61",
            # samples_per_frame=None (default) → auto-derive
            sample_period_us=5000,
        )
        stream = BLEImuGenericStream("imu", profile=profile, address="x")
        received = []
        stream.on_sample(received.append)

        # One sample — 4 bytes total (2B header + 2B body).
        stream._dispatch_notification_for_test(b"\x55\x61" + struct.pack("<h", 7))
        # Three samples bundled — 12 bytes total.
        bundled = b"".join(
            b"\x55\x61" + struct.pack("<h", v) for v in (11, 12, 13)
        )
        stream._dispatch_notification_for_test(bundled)

        assert [s.channels["v"] for s in received] == [7, 11, 12, 13]

    def test_timestamps_anchor_last_sample_at_receive_instant(self, mock_bleak):
        """The last sample in a bundled notification lands at recv_ns;
        earlier samples step backward in time by sample_period_us.
        This is the physically correct anchor — the sensor held earlier
        samples in its TX buffer before flushing the whole batch."""
        from syncfield.adapters.ble_imu import (
            BLEImuGenericStream,
            BLEImuProfile,
            ChannelSpec,
        )
        profile = BLEImuProfile(
            notify_uuid="c",
            struct_format="<h",
            channels=(ChannelSpec("v"),),
            sample_period_us=10_000,   # 10 ms between samples
        )
        stream = BLEImuGenericStream("imu", profile=profile, address="x")
        received = []
        stream.on_sample(received.append)

        before = time.monotonic_ns()
        stream._dispatch_notification_for_test(struct.pack("<hhh", 1, 2, 3))
        after = time.monotonic_ns()

        # Last sample timestamp is within the handler's wall-time window;
        # earlier samples are 10 ms and 20 ms earlier.
        assert before <= received[-1].capture_ns <= after
        assert received[-2].capture_ns == received[-1].capture_ns - 10_000_000
        assert received[-3].capture_ns == received[-1].capture_ns - 20_000_000

    def test_per_sample_header_mismatch_in_bundle_rejects_whole_payload(
        self, mock_bleak
    ):
        """If any sub-frame's header prefix doesn't match, the entire
        notification is dropped as WARNING — we don't partially decode."""
        from syncfield.adapters.ble_imu import (
            BLEImuGenericStream,
            BLEImuProfile,
            ChannelSpec,
        )
        profile = BLEImuProfile(
            notify_uuid="c",
            struct_format="<h",
            channels=(ChannelSpec("v"),),
            frame_header=b"\x55\x61",
            sample_period_us=5000,
        )
        stream = BLEImuGenericStream("imu", profile=profile, address="x")
        samples, healths = [], []
        stream.on_sample(samples.append)
        stream.on_health(healths.append)

        # Three samples; the middle one carries a bad header.
        payload = (
            b"\x55\x61" + struct.pack("<h", 1)
            + b"\xff\xff" + struct.pack("<h", 2)
            + b"\x55\x61" + struct.pack("<h", 3)
        )
        stream._dispatch_notification_for_test(payload)

        assert samples == []
        assert len(healths) == 1
        assert "sample 1" in healths[0].detail

    def test_preview_phase_emits_but_does_not_count_frames(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuGenericStream
        stream = BLEImuGenericStream(
            "imu", profile=_simple_profile(mock_bleak), address="x",
        )
        received = []
        stream.on_sample(received.append)

        # No start_recording — we are in the CONNECTED preview phase.
        payload = b"\xAA\xBB" + struct.pack("<hhh", 1, 2, 3)
        stream._dispatch_notification_for_test(payload)

        assert len(received) == 1                    # sample was emitted…
        assert received[0].frame_number == -1        # …but marked as preview
        assert stream._frame_count == 0              # counters untouched
        assert stream._first_at is None

    def test_recording_phase_advances_counters(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuGenericStream
        stream = BLEImuGenericStream(
            "imu", profile=_simple_profile(mock_bleak), address="x",
        )
        stream._recording = True    # skip the async lifecycle for unit test

        payload = b"\xAA\xBB" + struct.pack("<hhh", 1, 2, 3)
        stream._dispatch_notification_for_test(payload)
        stream._dispatch_notification_for_test(payload)

        assert stream._frame_count == 2
        assert stream._first_at is not None
        assert stream._last_at is not None


# ============================================================================
# Async lifecycle + config writes
# ============================================================================


class TestLifecycle:
    def test_connect_runs_config_writes_in_order_before_notify(self, mock_bleak):
        _, client = mock_bleak
        from syncfield.adapters.ble_imu import (
            BLEImuGenericStream,
            BLEImuProfile,
            ChannelSpec,
            ConfigWrite,
        )

        profile = BLEImuProfile(
            notify_uuid="data_char",
            struct_format="<h",
            channels=(ChannelSpec("v"),),
            config_writes=(
                ConfigWrite("cfg", b"\x01", delay_after_s=0),
                ConfigWrite("cfg", b"\x02", delay_after_s=0),
                ConfigWrite("cfg", b"\x03", delay_after_s=0),
            ),
        )
        stream = BLEImuGenericStream("imu", profile=profile, address="x")
        stream.connect()
        time.sleep(0.2)              # let the async loop run through connect()
        report = stream.stop()

        assert report.status == "completed"
        # All three config writes landed, in order, with the declared bytes.
        written = [c.args[1] for c in client.write_gatt_char.await_args_list]
        assert written == [b"\x01", b"\x02", b"\x03"]
        # Notify subscription targeted the profile's notify_uuid.
        assert client.start_notify.await_args.args[0] == "data_char"

    def test_legacy_start_stop_round_trip(self, mock_bleak):
        _, client = mock_bleak
        from syncfield.adapters.ble_imu import BLEImuGenericStream
        stream = BLEImuGenericStream(
            "imu", profile=_simple_profile(mock_bleak), address="x",
        )
        stream.prepare()
        stream.start(_clock())
        time.sleep(0.1)
        report = stream.stop()

        assert report.status == "completed"
        assert client.connect.await_count >= 1
        assert client.start_notify.await_count >= 1
        assert client.disconnect.await_count >= 1


# ============================================================================
# Optional-dep import guard
# ============================================================================


def test_bleak_missing_raises_clear_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "bleak", None)
    sys.modules.pop("syncfield.adapters.ble_imu", None)
    with pytest.raises(ImportError, match=r"syncfield\[ble\]"):
        importlib.import_module("syncfield.adapters.ble_imu")
