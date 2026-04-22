"""StartupFailureDetector — fires when connect/start_recording raises.

Relies on orchestrator-emitted HealthEvents with ``data["phase"]`` in
{``"connect"``, ``"start_recording"``}. A subsequent success event with
``data["outcome"] == "success"`` closes the incident.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Dict, List, Set

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import Incident
from syncfield.types import HealthEvent, HealthEventKind

_STARTUP_PHASES = {"connect", "start_recording"}


class StartupFailureDetector(DetectorBase):
    name = "startup-failure"
    default_severity = Severity.ERROR

    def __init__(self) -> None:
        self._pending_failures: Dict[str, HealthEvent] = {}
        self._recovered: Set[str] = set()

    def observe_health(self, stream_id: str, event: HealthEvent) -> None:
        phase = event.data.get("phase") if event.data else None
        if phase not in _STARTUP_PHASES:
            return
        outcome = event.data.get("outcome") if event.data else None
        if event.kind == HealthEventKind.ERROR and outcome != "success":
            self._pending_failures[stream_id] = event
            self._recovered.discard(stream_id)
        elif outcome == "success":
            self._recovered.add(stream_id)
            # Clear any stale pending failure so the next tick doesn't emit it
            # as a spurious post-recovery event.
            self._pending_failures.pop(stream_id, None)

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        out: List[HealthEvent] = []
        for stream_id, origin in list(self._pending_failures.items()):
            out.append(HealthEvent(
                stream_id=stream_id,
                kind=HealthEventKind.ERROR,
                at_ns=now_ns,
                detail=origin.detail or "Startup failure",
                severity=self.default_severity,
                source=f"detector:{self.name}",
                fingerprint=f"{stream_id}:{self.name}",
                data={"phase": origin.data.get("phase"), "origin_at_ns": origin.at_ns},
            ))
            del self._pending_failures[stream_id]
        return iter(out)

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        return incident.stream_id in self._recovered
