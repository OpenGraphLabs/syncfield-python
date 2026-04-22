"""Data classes for the health/incident layer.

These are plain, explicit structs — the :mod:`syncfield.health` runtime
mutates :class:`Incident` objects in-place on the worker thread. The
viewer receives immutable :class:`IncidentSnapshot`\\ s instead.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, List

from syncfield.health.severity import Severity, max_severity
from syncfield.types import HealthEvent


@dataclass(frozen=True)
class WriterStats:
    """One observation of a per-stream writer's queue."""

    stream_id: str
    at_ns: int
    queue_depth: int
    queue_capacity: int
    dropped: int

    @property
    def queue_fullness(self) -> float:
        if self.queue_capacity <= 0:
            return 0.0
        return self.queue_depth / self.queue_capacity


@dataclass(frozen=True)
class IncidentArtifact:
    """A piece of evidence attached to an Incident (crash dump, log excerpt, ...)."""

    kind: str
    path: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "path": self.path, "detail": self.detail}


@dataclass
class Incident:
    """A grouped, open/close-tracked sequence of HealthEvents sharing a fingerprint.

    Mutable because the worker thread updates ``last_event`` / ``event_count``
    / ``severity`` on every matching event. The viewer never sees this
    class directly — it reads :class:`IncidentSnapshot` instead.
    """

    id: str
    stream_id: str
    fingerprint: str
    title: str
    severity: Severity
    source: str
    opened_at_ns: int
    closed_at_ns: int | None
    last_event_at_ns: int
    event_count: int
    first_event: HealthEvent
    last_event: HealthEvent
    artifacts: List[IncidentArtifact] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def opened_from(cls, event: HealthEvent, *, title: str) -> "Incident":
        return cls(
            id=uuid.uuid4().hex,
            stream_id=event.stream_id,
            fingerprint=event.fingerprint,
            title=title,
            severity=event.severity,
            source=event.source,
            opened_at_ns=event.at_ns,
            closed_at_ns=None,
            last_event_at_ns=event.at_ns,
            event_count=1,
            first_event=event,
            last_event=event,
        )

    @property
    def is_open(self) -> bool:
        return self.closed_at_ns is None

    def record_event(self, event: HealthEvent) -> None:
        self.event_count += 1
        self.last_event = event
        self.last_event_at_ns = event.at_ns
        self.severity = max_severity(self.severity, event.severity)

    def close(self, *, at_ns: int) -> None:
        self.closed_at_ns = at_ns

    def attach(self, artifact: IncidentArtifact) -> None:
        self.artifacts.append(artifact)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stream_id": self.stream_id,
            "fingerprint": self.fingerprint,
            "title": self.title,
            "severity": self.severity.value,
            "source": self.source,
            "opened_at_ns": self.opened_at_ns,
            "closed_at_ns": self.closed_at_ns,
            "last_event_at_ns": self.last_event_at_ns,
            "event_count": self.event_count,
            "first_event": self.first_event.to_dict(),
            "last_event": self.last_event.to_dict(),
            "artifacts": [a.to_dict() for a in self.artifacts],
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class IncidentSnapshot:
    """Read-only view of an Incident, for the viewer's WebSocket payload."""

    id: str
    stream_id: str
    fingerprint: str
    title: str
    severity: str
    source: str
    opened_at_ns: int
    closed_at_ns: int | None
    event_count: int
    detail: str | None
    ago_s: float
    artifacts: List[dict]

    @property
    def is_open(self) -> bool:
        return self.closed_at_ns is None

    @classmethod
    def from_incident(cls, inc: Incident, *, now_ns: int) -> "IncidentSnapshot":
        anchor = inc.closed_at_ns if inc.closed_at_ns is not None else inc.last_event_at_ns
        ago_s = max(0.0, (now_ns - anchor) / 1e9)
        return cls(
            id=inc.id,
            stream_id=inc.stream_id,
            fingerprint=inc.fingerprint,
            title=inc.title,
            severity=inc.severity.value,
            source=inc.source,
            opened_at_ns=inc.opened_at_ns,
            closed_at_ns=inc.closed_at_ns,
            event_count=inc.event_count,
            detail=inc.last_event.detail,
            ago_s=ago_s,
            artifacts=[a.to_dict() for a in inc.artifacts],
        )
