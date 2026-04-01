"""Tests for syncfield.types."""

from syncfield.types import FrameTimestamp, SyncPoint


def test_sync_point_create_now():
    sp = SyncPoint.create_now("test_host")
    assert sp.host_id == "test_host"
    assert sp.monotonic_ns > 0
    assert sp.wall_clock_ns > 0
    assert sp.timestamp_ms > 0
    assert len(sp.iso_datetime) > 0


def test_sync_point_to_dict():
    sp = SyncPoint.create_now("h1")
    d = sp.to_dict()
    assert d["host_id"] == "h1"
    assert set(d.keys()) == {
        "monotonic_ns", "wall_clock_ns", "host_id", "timestamp_ms", "iso_datetime",
    }


def test_frame_timestamp_defaults():
    ts = FrameTimestamp(frame_number=0, capture_ns=123456789)
    assert ts.clock_source == "host_monotonic"
    assert ts.clock_domain == "local_host"
    assert ts.uncertainty_ns == 5_000_000


def test_frame_timestamp_to_dict():
    ts = FrameTimestamp(frame_number=5, capture_ns=999, clock_domain="rig_01")
    d = ts.to_dict()
    assert d == {
        "frame_number": 5,
        "capture_ns": 999,
        "clock_source": "host_monotonic",
        "clock_domain": "rig_01",
        "uncertainty_ns": 5_000_000,
    }


def test_frame_timestamp_round_trip():
    original = FrameTimestamp(
        frame_number=10,
        capture_ns=1234567890,
        clock_domain="my_host",
        uncertainty_ns=1_000_000,
    )
    restored = FrameTimestamp.from_dict(original.to_dict())
    assert restored.frame_number == original.frame_number
    assert restored.capture_ns == original.capture_ns
    assert restored.clock_domain == original.clock_domain
    assert restored.uncertainty_ns == original.uncertainty_ns


def test_frame_timestamp_from_dict_defaults():
    """from_dict should fill defaults for optional fields."""
    minimal = {"frame_number": 0, "capture_ns": 100}
    ts = FrameTimestamp.from_dict(minimal)
    assert ts.clock_source == "host_monotonic"
    assert ts.clock_domain == "local_host"
    assert ts.uncertainty_ns == 5_000_000
