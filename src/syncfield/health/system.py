"""HealthSystem — the single handle the orchestrator + user code touch."""

from __future__ import annotations

from typing import Callable, Dict, Iterable, Iterator, Optional

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
        self._target_hz_by_stream: Dict[str, Optional[float]] = {}

        self._install_default_detectors()

    # --- incident callbacks ----------------------------------------------

    def on_incident_opened(self, cb: Callable[[Incident], None]) -> None:
        """Register a callback fired when a new incident opens."""
        self._tracker.add_on_opened(cb)

    def on_incident_updated(self, cb: Callable[[Incident], None]) -> None:
        """Register a callback fired when an open incident receives a new event."""
        self._tracker.add_on_updated(cb)

    def on_incident_closed(self, cb: Callable[[Incident], None]) -> None:
        """Register a callback fired when an incident is resolved."""
        self._tracker.add_on_closed(cb)

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

    def register_stream(self, stream_id: str, target_hz: Optional[float]) -> None:
        """Declare the expected target_hz for a stream. Detectors consult this."""
        self._target_hz_by_stream[stream_id] = target_hz

    def _install_default_detectors(self) -> None:
        target_getter = lambda sid: self._target_hz_by_stream.get(sid)
        self.register(AdapterEventPassthrough())
        self.register(StreamStallDetector())
        self.register(FpsDropDetector(target_getter=target_getter))
        self.register(JitterDetector(target_getter=target_getter))
        self.register(StartupFailureDetector())
        self.register(BackpressureDetector())
