"""IncidentTracker — groups HealthEvents into Incidents and manages open/close.

Runs on the HealthWorker thread. Public methods are *not* thread-safe on
their own; the worker serializes access.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from syncfield.health.detector import Detector
from syncfield.health.types import Incident
from syncfield.types import HealthEvent

Callback = Callable[[Incident], None]


class IncidentTracker:
    def __init__(self, passthrough_close_ns: int = 30 * 1_000_000_000) -> None:
        self._by_fingerprint: Dict[str, Incident] = {}
        self._resolved: List[Incident] = []
        self._detectors_by_name: Dict[str, Detector] = {}
        self._passthrough_close_ns = passthrough_close_ns

        self.on_opened: Optional[Callback] = None
        self.on_updated: Optional[Callback] = None
        self.on_closed: Optional[Callback] = None

    # --- detector wiring -------------------------------------------------

    def bind_detector(self, detector: Detector) -> None:
        self._detectors_by_name[detector.name] = detector

    # --- event ingestion -------------------------------------------------

    def ingest(self, event: HealthEvent) -> None:
        if not event.fingerprint:
            raise ValueError(
                "HealthEvent.fingerprint is required before reaching the IncidentTracker; "
                "the platform fills it in for adapter events, detectors set their own."
            )
        open_inc = self._by_fingerprint.get(event.fingerprint)
        if open_inc is None:
            inc = Incident.opened_from(event, title=_title_from(event))
            self._by_fingerprint[event.fingerprint] = inc
            self._fire(self.on_opened, inc)
            return
        open_inc.record_event(event)
        self._fire(self.on_updated, open_inc)

    # --- tick — evaluate close conditions --------------------------------

    def tick(self, now_ns: int) -> None:
        to_close: List[str] = []
        for fp, inc in self._by_fingerprint.items():
            detector = self._detector_for(inc)
            if detector is not None:
                should_close = detector.close_condition(inc, now_ns)
            else:
                should_close = (now_ns - inc.last_event_at_ns) >= self._passthrough_close_ns
            if should_close:
                to_close.append(fp)

        for fp in to_close:
            inc = self._by_fingerprint.pop(fp)
            inc.close(at_ns=now_ns)
            self._resolved.append(inc)
            self._fire(self.on_closed, inc)

    def close_all(self, *, at_ns: int) -> None:
        """Used at session stop to resolve any still-open incidents."""
        for fp in list(self._by_fingerprint.keys()):
            inc = self._by_fingerprint.pop(fp)
            inc.close(at_ns=at_ns)
            self._resolved.append(inc)
            self._fire(self.on_closed, inc)

    # --- read-only views -------------------------------------------------

    def open_incidents(self) -> List[Incident]:
        return list(self._by_fingerprint.values())

    def resolved_incidents(self) -> List[Incident]:
        return list(self._resolved)

    # --- helpers ---------------------------------------------------------

    def _detector_for(self, inc: Incident) -> Optional[Detector]:
        # Fingerprint convention: "<stream_id>:<detector_name>[:suffix]".
        parts = inc.fingerprint.split(":", 2)
        if len(parts) < 2:
            return None
        return self._detectors_by_name.get(parts[1])

    @staticmethod
    def _fire(cb: Optional[Callback], inc: Incident) -> None:
        if cb is not None:
            cb(inc)


def _title_from(event: HealthEvent) -> str:
    # Prefer the first event's detail as the title; fall back to
    # "<source>: <fingerprint>" if the detail is missing.
    if event.detail:
        return event.detail
    return f"{event.source}: {event.fingerprint}"
