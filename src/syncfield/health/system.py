"""HealthSystem — the single handle the orchestrator + user code touch."""

from __future__ import annotations

from typing import Callable, Iterable, Iterator, Optional

from syncfield.health.detector import Detector
from syncfield.health.detectors.adapter_passthrough import AdapterEventPassthrough
from syncfield.health.detectors.backpressure import BackpressureDetector
from syncfield.health.detectors.fps_drop import FpsDropDetector
from syncfield.health.detectors.jitter import JitterDetector
from syncfield.health.detectors.startup_failure import StartupFailureDetector
from syncfield.health.detectors.stream_stall import StreamStallDetector
from syncfield.health.registry import DetectorRegistry
from syncfield.health.tracker import IncidentTracker
from syncfield.health.types import Incident, WriterStats
from syncfield.health.worker import HealthWorker
from syncfield.types import HealthEvent, SampleEvent, SessionState


class HealthSystem:
    """Composes Registry + Tracker + Worker into a single user-facing facade."""

    def __init__(
        self,
        *,
        tick_hz: float = 20.0,
        passthrough_close_ns: int = 30 * 1_000_000_000,
    ) -> None:
        self._registry = DetectorRegistry()
        self._tracker = IncidentTracker(passthrough_close_ns=passthrough_close_ns)
        self._worker: Optional[HealthWorker] = None
        self._tick_hz = tick_hz

        self.on_incident_opened: Optional[Callable[[Incident], None]] = None
        self.on_incident_updated: Optional[Callable[[Incident], None]] = None
        self.on_incident_closed: Optional[Callable[[Incident], None]] = None

        self._tracker.on_opened = lambda inc: self._fire("on_incident_opened", inc)
        self._tracker.on_updated = lambda inc: self._fire("on_incident_updated", inc)
        self._tracker.on_closed = lambda inc: self._fire("on_incident_closed", inc)

        self._install_default_detectors()

    # --- registry --------------------------------------------------------

    def register(self, detector: Detector) -> None:
        import warnings
        self._registry.register(detector)
        self._tracker.bind_detector(detector)
        if self._worker is not None:
            warnings.warn(
                f"Detector '{detector.name}' registered after HealthSystem.start(); "
                "it will be bound for close-condition routing but will NOT be ticked "
                "until the system is stopped and restarted.",
                RuntimeWarning,
                stacklevel=2,
            )

    def unregister(self, name: str) -> None:
        self._registry.unregister(name)

    def iter_detectors(self) -> Iterator[Detector]:
        return iter(self._registry)

    # --- observer inputs -------------------------------------------------

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None:
        if self._worker is not None:
            self._worker.push_sample(stream_id, sample)

    def observe_health(self, stream_id: str, event: HealthEvent) -> None:
        if self._worker is not None:
            self._worker.push_health(stream_id, event)

    def observe_state(self, old: SessionState, new: SessionState) -> None:
        if self._worker is not None:
            self._worker.push_state(old, new)

    def observe_writer_stats(self, stream_id: str, stats: WriterStats) -> None:
        if self._worker is not None:
            self._worker.push_writer_stats(stream_id, stats)

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._worker is not None:
            return  # already running — idempotent
        self._worker = HealthWorker(
            tracker=self._tracker,
            detectors=list(self._registry),
            tick_hz=self._tick_hz,
        )
        self._worker.start()

    def stop(self, *, close_open_incidents: bool = True, now_ns: Optional[int] = None) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker = None
        if close_open_incidents:
            import time
            self._tracker.close_all(at_ns=now_ns if now_ns is not None else time.monotonic_ns())

    # --- read-only views -------------------------------------------------

    def open_incidents(self) -> Iterable[Incident]:
        return self._tracker.open_incidents()

    def resolved_incidents(self) -> Iterable[Incident]:
        return self._tracker.resolved_incidents()

    # --- helpers ---------------------------------------------------------

    def _install_default_detectors(self) -> None:
        self.register(AdapterEventPassthrough())
        self.register(StreamStallDetector())
        self.register(FpsDropDetector())
        self.register(JitterDetector())
        self.register(StartupFailureDetector())
        self.register(BackpressureDetector())

    def _fire(self, attr: str, inc: Incident) -> None:
        cb = getattr(self, attr, None)
        if cb is not None:
            cb(inc)
