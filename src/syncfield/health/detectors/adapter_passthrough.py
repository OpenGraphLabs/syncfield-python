"""Passthrough detector: owns close semantics for adapter-emitted events.

Fingerprint convention ``<stream_id>:adapter:<subkind>`` routes to this
detector in the tracker. It never emits synthetic events; its only job
is saying "if no new adapter event arrived for N seconds, close the
incident".
"""

from __future__ import annotations

from syncfield.health.detector import DetectorBase
from syncfield.health.severity import Severity
from syncfield.health.types import Incident


class AdapterEventPassthrough(DetectorBase):
    name = "adapter"
    default_severity = Severity.WARNING

    def __init__(self, quiet_ns: int = 30 * 1_000_000_000) -> None:
        self._quiet_ns = quiet_ns

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        return (now_ns - incident.last_event_at_ns) >= self._quiet_ns
