"""StreamStallDetector — fires when a stream stops producing samples.

Only tracks streams that are currently in the ``connected`` connection
state. When a stream transitions away from ``connected`` (e.g. during
``stop()`` teardown or an explicit ``disconnect()``), bookkeeping is
cleared so the inevitable post-teardown silence does not raise a
spurious ERROR-severity incident every time a recording ends.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Dict, List, Set

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
        # Streams currently in the ``connected`` state; only these are
        # tracked. Disconnected/stopping streams are silent by design.
        self._connected: Set[str] = set()

    # --- observers -------------------------------------------------------

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None:
        self._last_sample_at[stream_id] = sample.capture_ns
        # A new sample ends any active stall bookkeeping.
        self._stall_active[stream_id] = False

    def observe_connection_state(self, stream_id: str, new_state: str, at_ns: int) -> None:
        if new_state == "connected":
            self._connected.add(stream_id)
            # Treat the connect time as the "last sample" anchor so the
            # detector waits a full threshold from connect, not from a
            # stale capture_ns left over from the previous session.
            self._last_sample_at[stream_id] = at_ns
            self._stall_active[stream_id] = False
        else:
            # idle / connecting / failed / disconnected / stopping →
            # stop tracking. NoDataDetector handles the warmup window;
            # post-teardown silence is expected and not an incident.
            self._connected.discard(stream_id)
            self._last_sample_at.pop(stream_id, None)
            self._stall_active.pop(stream_id, None)

    # --- tick ------------------------------------------------------------

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        emitted: List[HealthEvent] = []
        for stream_id in self._connected:
            last = self._last_sample_at.get(stream_id)
            if last is None:
                continue
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
        # Stream no longer connected: close — its silence is no longer
        # this detector's concern.
        if incident.stream_id not in self._connected:
            return True
        last = self._last_sample_at.get(incident.stream_id)
        if last is None:
            return False
        return (now_ns - last) < self._stall_threshold_ns \
            and (now_ns - incident.last_event_at_ns) >= self._recovery_ns
