"""NoDataDetector — fires when a stream is connected but never emits a sample.

Complements StreamStallDetector (which requires prior samples). Catches
the "connected but silent" case such as an OAK pipeline that fails to
pump frames even though its device connected. Resets bookkeeping on
any non-connected state transition so a reconnect starts a fresh clock.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Dict, List, Set

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import Incident
from syncfield.types import HealthEvent, HealthEventKind, SampleEvent


class NoDataDetector(DetectorBase):
    name = "no-data"
    default_severity = Severity.ERROR

    # 30s default: CoreAudio host_audio first-mic-permission + device
    # open + first-callback can take 15-25s on a cold macOS process,
    # and slower hardware (BLE IMU pairing, network-attached cameras)
    # can be slower still. Anything that fires for normal warmup
    # drowns the Active Issues panel in self-resolving noise. The
    # detector is for catching genuine "connected but silent
    # forever" cases, not warmup latency.
    def __init__(self, threshold_ns: int = 30_000_000_000) -> None:
        self._threshold_ns = threshold_ns
        self._connected_at: Dict[str, int] = {}
        self._has_sample: Set[str] = set()
        self._fire_active: Dict[str, bool] = {}

    def observe_connection_state(self, stream_id: str, new_state: str, at_ns: int) -> None:
        if new_state == "connected":
            self._connected_at[stream_id] = at_ns
            self._has_sample.discard(stream_id)
            self._fire_active[stream_id] = False
        else:
            # idle / connecting / failed / disconnected → reset everything.
            self._connected_at.pop(stream_id, None)
            self._has_sample.discard(stream_id)
            self._fire_active.pop(stream_id, None)

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None:
        self._has_sample.add(stream_id)

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        out: List[HealthEvent] = []
        for stream_id, connected_at in self._connected_at.items():
            if stream_id in self._has_sample:
                continue
            elapsed = now_ns - connected_at
            if elapsed >= self._threshold_ns and not self._fire_active.get(stream_id, False):
                self._fire_active[stream_id] = True
                out.append(HealthEvent(
                    stream_id=stream_id,
                    kind=HealthEventKind.ERROR,
                    at_ns=now_ns,
                    detail=f"Connected {elapsed / 1e9:.1f}s ago but no data received",
                    severity=self.default_severity,
                    source=f"detector:{self.name}",
                    fingerprint=f"{stream_id}:{self.name}",
                    data={"connected_at_ns": connected_at, "elapsed_ns": elapsed},
                ))
        return iter(out)

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        return incident.stream_id in self._has_sample
