"""BackpressureDetector — writer queue saturation + drop-counter detector."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Dict, List, Optional

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import Incident, WriterStats
from syncfield.types import HealthEvent, HealthEventKind


class BackpressureDetector(DetectorBase):
    name = "backpressure"
    default_severity = Severity.WARNING

    def __init__(
        self,
        fullness_threshold: float = 0.80,
        sustain_ns: int = 2_000_000_000,
        recovery_ratio: float = 0.30,
        recovery_ns: int = 5_000_000_000,
    ) -> None:
        self._threshold = fullness_threshold
        self._sustain_ns = sustain_ns
        self._recovery_ratio = recovery_ratio
        self._recovery_ns = recovery_ns

        self._latest: Dict[str, WriterStats] = {}
        self._first_bad_observed_at: Dict[str, Optional[int]] = {}
        self._last_dropped: Dict[str, int] = {}
        self._pending_drop_spike: Dict[str, bool] = {}
        self._fire_active: Dict[str, bool] = {}
        self._recovery_began_at: Dict[str, Optional[int]] = {}

    def observe_writer_stats(self, stream_id: str, stats: WriterStats) -> None:
        self._latest[stream_id] = stats
        prev = self._last_dropped.get(stream_id, 0)
        if stats.dropped > prev:
            self._pending_drop_spike[stream_id] = True
        self._last_dropped[stream_id] = stats.dropped

        # Track when fullness first exceeds threshold
        if stats.queue_fullness >= self._threshold:
            if self._first_bad_observed_at.get(stream_id) is None:
                self._first_bad_observed_at[stream_id] = stats.at_ns
        else:
            self._first_bad_observed_at[stream_id] = None

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        out: List[HealthEvent] = []
        for stream_id, stats in self._latest.items():
            fire_now = False
            detail = ""

            if self._pending_drop_spike.pop(stream_id, False):
                fire_now = True
                detail = f"Writer dropped frames (total {stats.dropped})"

            if stats.queue_fullness >= self._threshold:
                began = self._first_bad_observed_at.get(stream_id)
                if began is not None:
                    if (now_ns - began) >= self._sustain_ns and not self._fire_active.get(stream_id, False):
                        fire_now = True
                        self._fire_active[stream_id] = True
                        detail = f"Writer queue {stats.queue_depth}/{stats.queue_capacity} full"
            else:
                self._fire_active[stream_id] = False

            if fire_now:
                out.append(HealthEvent(
                    stream_id=stream_id,
                    kind=HealthEventKind.WARNING,
                    at_ns=now_ns,
                    detail=detail,
                    severity=self.default_severity,
                    source=f"detector:{self.name}",
                    fingerprint=f"{stream_id}:{self.name}",
                    data={
                        "queue_depth": stats.queue_depth,
                        "queue_capacity": stats.queue_capacity,
                        "dropped": stats.dropped,
                        "dropped_at_open": stats.dropped,
                    },
                ))
        return iter(out)

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        stats = self._latest.get(incident.stream_id)
        if stats is None:
            return False
        if stats.queue_fullness > self._recovery_ratio:
            self._recovery_began_at[incident.stream_id] = None
            return False
        # No new drops since incident opened.
        opened_dropped = incident.data.get("dropped_at_open")
        current_dropped = self._last_dropped.get(incident.stream_id, 0)
        if opened_dropped is not None and current_dropped > opened_dropped:
            return False
        began = self._recovery_began_at.get(incident.stream_id)
        if began is None:
            self._recovery_began_at[incident.stream_id] = now_ns
            return False
        return (now_ns - began) >= self._recovery_ns
