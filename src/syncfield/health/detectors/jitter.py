"""JitterDetector — p95-based inter-sample interval anomaly detector."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from typing import Callable, Deque, Dict, List, Optional

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import Incident
from syncfield.types import HealthEvent, HealthEventKind, SampleEvent

TargetGetter = Callable[[str], Optional[float]]


def _p95(values: List[int]) -> int:
    if not values:
        return 0
    sorted_v = sorted(values)
    idx = max(0, int(0.95 * (len(sorted_v) - 1)))
    return sorted_v[idx]


class JitterDetector(DetectorBase):
    name = "jitter"
    default_severity = Severity.WARNING

    def __init__(
        self,
        target_getter: TargetGetter = lambda sid: None,
        window: int = 60,
        jitter_ratio: float = 2.0,
        sustain_ns: int = 3_000_000_000,
        recovery_ratio: float = 1.2,
        recovery_ns: int = 10_000_000_000,
    ) -> None:
        self._target_getter = target_getter
        self._window = window
        self._jitter_ratio = jitter_ratio
        self._sustain_ns = sustain_ns
        self._recovery_ratio = recovery_ratio
        self._recovery_ns = recovery_ns

        self._last_at: Dict[str, int] = {}
        self._intervals: Dict[str, Deque[int]] = {}
        self._bad_began_at: Dict[str, Optional[int]] = {}
        self._fire_active: Dict[str, bool] = {}
        self._recovery_began_at: Dict[str, Optional[int]] = {}

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None:
        last = self._last_at.get(stream_id)
        if last is not None:
            buf = self._intervals.setdefault(stream_id, deque(maxlen=self._window))
            buf.append(sample.capture_ns - last)
        self._last_at[stream_id] = sample.capture_ns

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        out: List[HealthEvent] = []
        for stream_id, buf in list(self._intervals.items()):
            target_hz = self._target_getter(stream_id)
            if target_hz is None or target_hz <= 0 or len(buf) < max(10, self._window // 2):
                continue
            expected = 1e9 / target_hz
            p95 = _p95(list(buf))

            if p95 > expected * self._jitter_ratio:
                began = self._bad_began_at.get(stream_id)
                if began is None:
                    # Backdate bad_began_at to the start of the current window.
                    # This is the sum of all intervals in the buffer, which represents
                    # the elapsed time from the start of this window to now.
                    window_elapsed = sum(buf)
                    self._bad_began_at[stream_id] = now_ns - window_elapsed
                    began = self._bad_began_at[stream_id]

                elapsed = now_ns - began
                if elapsed >= self._sustain_ns and not self._fire_active.get(stream_id, False):
                    self._fire_active[stream_id] = True
                    out.append(HealthEvent(
                        stream_id=stream_id,
                        kind=HealthEventKind.WARNING,
                        at_ns=now_ns,
                        detail=f"Jitter spike (p95 {p95/1e6:.1f} ms, expected {expected/1e6:.1f} ms)",
                        severity=self.default_severity,
                        source=f"detector:{self.name}",
                        fingerprint=f"{stream_id}:{self.name}",
                        data={"p95_ns": p95, "expected_ns": int(expected)},
                    ))
            else:
                self._bad_began_at[stream_id] = None
                self._fire_active[stream_id] = False
        return iter(out)

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        stream_id = incident.stream_id
        buf = self._intervals.get(stream_id)
        target_hz = self._target_getter(stream_id)
        if not buf or target_hz is None or target_hz <= 0:
            return False
        expected = 1e9 / target_hz
        p95 = _p95(list(buf))
        if p95 > expected * self._recovery_ratio:
            self._recovery_began_at[stream_id] = None
            return False
        began = self._recovery_began_at.get(stream_id)
        if began is None:
            self._recovery_began_at[stream_id] = now_ns
            return False
        return (now_ns - began) >= self._recovery_ns
