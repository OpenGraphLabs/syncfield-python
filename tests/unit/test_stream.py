"""Tests for the Stream protocol and StreamBase helper class."""

from __future__ import annotations

from typing import List

from syncfield.clock import SessionClock
from syncfield.stream import Stream, StreamBase
from syncfield.types import (
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    StreamCapabilities,
    SyncPoint,
)


class _DemoStream(StreamBase):
    """Minimal concrete Stream used to exercise the base class behavior."""

    def __init__(self, id: str) -> None:
        super().__init__(
            id=id,
            kind="sensor",
            capabilities=StreamCapabilities(supports_precise_timestamps=True),
        )
        self.prepared = False
        self.started = False
        self.stopped = False
        self._clock: SessionClock | None = None

    def prepare(self) -> None:
        self.prepared = True

    def start(self, session_clock: SessionClock) -> None:
        self.started = True
        self._clock = session_clock

    def stop(self) -> FinalizationReport:
        self.stopped = True
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=0,
            file_path=None,
            first_sample_at_ns=None,
            last_sample_at_ns=None,
            health_events=list(self._collected_health),
            error=None,
        )


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


def test_stream_protocol_is_runtime_checkable():
    assert isinstance(_DemoStream("x"), Stream)


def test_demo_stream_lifecycle():
    demo = _DemoStream("x")
    demo.prepare()
    assert demo.prepared
    demo.start(_clock())
    assert demo.started
    report = demo.stop()
    assert demo.stopped
    assert report.stream_id == "x"
    assert report.status == "completed"


def test_stream_base_routes_sample_events_to_callback():
    demo = _DemoStream("x")
    received: List[SampleEvent] = []
    demo.on_sample(received.append)
    ev = SampleEvent(stream_id="x", frame_number=1, capture_ns=1000)
    demo._emit_sample(ev)
    assert received == [ev]


def test_stream_base_routes_health_to_callback_and_buffer():
    demo = _DemoStream("x")
    received: List[HealthEvent] = []
    demo.on_health(received.append)
    ev = HealthEvent("x", HealthEventKind.HEARTBEAT, at_ns=100)
    demo._emit_health(ev)
    assert received == [ev]
    # Also accumulated internally for inclusion in FinalizationReport
    report = demo.stop()
    assert ev in report.health_events


def test_stream_base_supports_multiple_sample_callbacks():
    demo = _DemoStream("x")
    calls_a: List[SampleEvent] = []
    calls_b: List[SampleEvent] = []
    demo.on_sample(calls_a.append)
    demo.on_sample(calls_b.append)
    ev = SampleEvent("x", 0, 0)
    demo._emit_sample(ev)
    assert calls_a == [ev]
    assert calls_b == [ev]


def test_stream_base_exposes_id_kind_capabilities():
    demo = _DemoStream("sensor_42")
    assert demo.id == "sensor_42"
    assert demo.kind == "sensor"
    assert demo.capabilities.supports_precise_timestamps is True


class TestDeviceKey:
    """device_key is the physical-device identity used for dedup."""

    def test_default_is_none(self):
        """StreamBase subclasses without hardware default to None."""
        assert _DemoStream("x").device_key is None

    def test_override_returns_tuple(self):
        """Adapters can advertise a stable (adapter_type, device_id) tuple."""

        class _UvcLike(StreamBase):
            def __init__(self, id: str, idx: int) -> None:
                super().__init__(
                    id=id,
                    kind="video",
                    capabilities=StreamCapabilities(),
                )
                self._idx = idx

            @property
            def device_key(self):
                return ("uvc_webcam", str(self._idx))

        assert _UvcLike("cam", 0).device_key == ("uvc_webcam", "0")
        assert _UvcLike("cam", 1).device_key == ("uvc_webcam", "1")
        # Same adapter, different indices → distinct keys.
        assert _UvcLike("a", 0).device_key != _UvcLike("b", 1).device_key
