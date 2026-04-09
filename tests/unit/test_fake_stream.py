"""Tests for the FakeStream test utility."""

from __future__ import annotations

import pytest

from syncfield.clock import SessionClock
from syncfield.stream import Stream
from syncfield.testing import FakeStream
from syncfield.types import HealthEvent, HealthEventKind, SampleEvent, SyncPoint


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


def test_fake_stream_satisfies_stream_protocol():
    assert isinstance(FakeStream("cam"), Stream)


def test_lifecycle_call_counts():
    fs = FakeStream("cam")
    fs.prepare()
    assert fs.prepare_calls == 1
    fs.start(_clock())
    assert fs.start_calls == 1
    report = fs.stop()
    assert fs.stop_calls == 1
    assert report.status == "completed"
    assert report.frame_count == 0


def test_push_sample_routes_to_callback_and_counts():
    fs = FakeStream("cam")
    fs.prepare()
    fs.start(_clock())
    received: list[SampleEvent] = []
    fs.on_sample(received.append)
    fs.push_sample(frame_number=0, capture_ns=1000)
    fs.push_sample(frame_number=1, capture_ns=2000)
    report = fs.stop()
    assert len(received) == 2
    assert received[0].frame_number == 0
    assert report.frame_count == 2
    assert report.first_sample_at_ns == 1000
    assert report.last_sample_at_ns == 2000


def test_push_health_routes_to_callback():
    fs = FakeStream("cam")
    fs.prepare()
    fs.start(_clock())
    received: list[HealthEvent] = []
    fs.on_health(received.append)
    fs.push_health(HealthEventKind.WARNING, at_ns=42, detail="test")
    fs.stop()
    assert len(received) == 1
    assert received[0].kind is HealthEventKind.WARNING
    assert received[0].detail == "test"


def test_fail_on_prepare_raises():
    fs = FakeStream("cam", fail_on_prepare=True)
    with pytest.raises(RuntimeError, match="fake failure"):
        fs.prepare()


def test_fail_on_start_raises():
    fs = FakeStream("cam", fail_on_start=True)
    fs.prepare()
    with pytest.raises(RuntimeError, match="fake failure"):
        fs.start(_clock())


def test_fail_on_stop_returns_failed_report():
    fs = FakeStream("cam", fail_on_stop=True)
    fs.prepare()
    fs.start(_clock())
    report = fs.stop()
    assert report.status == "failed"
    assert report.error is not None


def test_audio_capability_flag():
    fs = FakeStream("cam", provides_audio_track=True)
    assert fs.capabilities.provides_audio_track is True
