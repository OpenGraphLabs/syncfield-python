"""FpsDropDetector — target-relative or baseline-learning FPS drop detector."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from typing import Callable, Deque, Dict, List, Optional

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import Incident
from syncfield.types import HealthEvent, HealthEventKind, SampleEvent

TargetGetter = Callable[[str], Optional[float]]

_WINDOW_NS = 1_000_000_000  # rolling 1s FPS window


class FpsDropDetector(DetectorBase):
    name = "fps-drop"
    default_severity = Severity.WARNING

    def __init__(
        self,
        target_getter: TargetGetter = lambda sid: None,
        drop_ratio: float = 0.70,
        sustain_ns: int = 3_000_000_000,
        recovery_ratio: float = 0.90,
        recovery_ns: int = 5_000_000_000,
        baseline_warmup_ns: int = 5_000_000_000,
        baseline_window_ns: int = 10_000_000_000,
    ) -> None:
        self._target_getter = target_getter
        self._drop_ratio = drop_ratio
        self._sustain_ns = sustain_ns
        self._recovery_ratio = recovery_ratio
        self._recovery_ns = recovery_ns
        self._baseline_warmup_ns = baseline_warmup_ns
        self._baseline_window_ns = baseline_window_ns

        self._samples: Dict[str, Deque[int]] = {}
        self._first_seen_at: Dict[str, int] = {}
        self._baseline: Dict[str, float] = {}
        self._baseline_locked: Dict[str, bool] = {}  # True once baseline is frozen
        # When did the stream first drop below threshold in the current dip?
        self._dip_began_at: Dict[str, Optional[int]] = {}
        self._fire_active: Dict[str, bool] = {}
        # Same thing for recovery tracking.
        self._recovery_began_at: Dict[str, Optional[int]] = {}
        # Track the most recent observation, for incremental dip tracking
        self._last_observed_state: Dict[str, bool] = {}  # True if in drop, False if not

    # --- observers -------------------------------------------------------

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None:
        buf = self._samples.setdefault(stream_id, deque())
        buf.append(sample.capture_ns)
        self._first_seen_at.setdefault(stream_id, sample.capture_ns)
        # Trim older than baseline_window.
        cutoff = sample.capture_ns - self._baseline_window_ns
        while buf and buf[0] < cutoff:
            buf.popleft()

    # --- tick ------------------------------------------------------------

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        out: List[HealthEvent] = []
        for stream_id, buf in list(self._samples.items()):
            target = self._effective_target(stream_id, now_ns)
            observed = self._observed_fps(buf, now_ns)
            if target is None or observed is None:
                continue

            if observed < target * self._drop_ratio:
                began = self._dip_began_at.get(stream_id)
                if began is None:
                    # Find when the drop started by looking at the buffer
                    dip_start = self._find_dip_start(buf, target, now_ns)
                    self._dip_began_at[stream_id] = dip_start
                    began = dip_start
                elapsed = now_ns - began
                if elapsed >= self._sustain_ns and not self._fire_active.get(stream_id, False):
                    self._fire_active[stream_id] = True
                    out.append(HealthEvent(
                        stream_id=stream_id,
                        kind=HealthEventKind.WARNING,
                        at_ns=now_ns,
                        detail=f"FPS drop ({observed:.1f} Hz, target {target:.1f} Hz)",
                        severity=self.default_severity,
                        source=f"detector:{self.name}",
                        fingerprint=f"{stream_id}:{self.name}",
                        data={"observed_hz": observed, "target_hz": target},
                    ))
            else:
                self._dip_began_at[stream_id] = None
                self._fire_active[stream_id] = False
        return iter(out)

    # --- close condition -------------------------------------------------

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        stream_id = incident.stream_id
        target = self._effective_target(stream_id, now_ns)
        observed = self._observed_fps(self._samples.get(stream_id, deque()), now_ns)
        if target is None or observed is None:
            return False
        if observed < target * self._recovery_ratio:
            self._recovery_began_at[stream_id] = None
            return False
        began = self._recovery_began_at.get(stream_id)
        if began is None:
            self._recovery_began_at[stream_id] = now_ns
            return False
        return (now_ns - began) >= self._recovery_ns

    # --- helpers ---------------------------------------------------------

    def _find_dip_start(self, buf: Deque[int], target: float, now_ns: int) -> int:
        """Find the earliest time when FPS dropped below threshold."""
        if not buf:
            return now_ns
        # Binary search to find when we first dropped below target
        threshold = target * self._drop_ratio
        left, right = 0, len(buf) - 1
        result = buf[-1] if buf else now_ns

        while left <= right:
            mid = (left + right) // 2
            # Check FPS from buf[mid] onwards
            mid_time = buf[mid]
            fps_from_mid = self._observed_fps_from(buf, mid_time, now_ns)

            if fps_from_mid < threshold:
                result = mid_time
                right = mid - 1
            else:
                left = mid + 1

        return result

    def _observed_fps_from(self, buf: Deque[int], start_time: int, now_ns: int) -> Optional[float]:
        """Calculate FPS from a specific start time to now."""
        if not buf:
            return None
        count = sum(1 for t in buf if t >= start_time and t <= now_ns)
        if count == 0:
            return 0.0
        elapsed_ns = now_ns - start_time
        if elapsed_ns < _WINDOW_NS:
            # Less than 1 second, use actual window
            return count / (elapsed_ns / 1e9) if elapsed_ns > 0 else 0.0
        else:
            # More than 1 second, use 1-second rolling window from now
            cutoff = now_ns - _WINDOW_NS
            count = sum(1 for t in buf if t >= cutoff)
            return count / (_WINDOW_NS / 1e9)

    def _effective_target(self, stream_id: str, now_ns: int) -> Optional[float]:
        declared = self._target_getter(stream_id)
        if declared is not None:
            return float(declared)
        first = self._first_seen_at.get(stream_id)
        if first is None:
            return None
        if (now_ns - first) < self._baseline_warmup_ns:
            return None
        cached = self._baseline.get(stream_id)
        if cached is not None:
            return cached
        # Calculate and lock baseline once warmup completes
        # Lock it to the FPS observed right after warmup completes
        if not self._baseline_locked.get(stream_id, False):
            buf = self._samples.get(stream_id, deque())
            if not buf:
                return None
            # Calculate FPS from immediately after warmup to now
            warmup_end = first + self._baseline_warmup_ns
            count = sum(1 for t in buf if t >= warmup_end)
            if count == 0:
                return None
            # Use the available time since warmup end
            elapsed = min(now_ns - warmup_end, self._baseline_window_ns)
            if elapsed <= 0:
                return None
            observed = count / (elapsed / 1e9)
            self._baseline[stream_id] = observed
            self._baseline_locked[stream_id] = True
        return self._baseline.get(stream_id)

    @staticmethod
    def _observed_fps(buf: Deque[int], now_ns: int) -> Optional[float]:
        if not buf:
            return None
        cutoff = now_ns - _WINDOW_NS
        count = sum(1 for t in buf if t >= cutoff)
        if count == 0:
            return 0.0
        return count / (_WINDOW_NS / 1e9)
