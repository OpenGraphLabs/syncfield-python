"""StreamStallDetector — fires when a stream stops producing samples."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Dict, List

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import Incident
from syncfield.types import HealthEvent, HealthEventKind, SampleEvent


class StreamStallDetector(DetectorBase):
    name = "stream-stall"
    default_severity = Severity.ERROR

    def __init__(
        self,
        stall_threshold_ns: int = 2_000_000_000,
        recovery_ns: int = 1_000_000_000,
    ) -> None:
        self._stall_threshold_ns = stall_threshold_ns
        self._recovery_ns = recovery_ns
        # Per-stream most-recent sample monotonic time.
        self._last_sample_at: Dict[str, int] = {}
        # Per-stream: are we currently firing? prevents duplicates per stall.
        self._stall_active: Dict[str, bool] = {}

    # --- observers -------------------------------------------------------

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None:
        self._last_sample_at[stream_id] = sample.capture_ns
        # A new sample ends any active stall bookkeeping.
        self._stall_active[stream_id] = False

    # --- tick ------------------------------------------------------------

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        emitted: List[HealthEvent] = []
        for stream_id, last in self._last_sample_at.items():
            silence_ns = now_ns - last
            if silence_ns >= self._stall_threshold_ns and not self._stall_active.get(stream_id, False):
                self._stall_active[stream_id] = True
                emitted.append(HealthEvent(
                    stream_id=stream_id,
                    kind=HealthEventKind.ERROR,
                    at_ns=now_ns,
                    detail=f"Stream stalled (silence {silence_ns / 1e9:.1f}s)",
                    severity=self.default_severity,
                    source=f"detector:{self.name}",
                    fingerprint=f"{stream_id}:{self.name}",
                    data={"silence_ns": silence_ns},
                ))
        return iter(emitted)

    # --- close condition -------------------------------------------------

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        last = self._last_sample_at.get(incident.stream_id)
        if last is None:
            return False
        return (now_ns - last) < self._stall_threshold_ns \
            and (now_ns - incident.last_event_at_ns) >= self._recovery_ns
