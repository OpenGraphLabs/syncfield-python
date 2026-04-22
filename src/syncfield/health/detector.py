"""Detector protocol + base class.

A :class:`Detector` observes the stream (samples, adapter health events,
session state, writer stats) and may emit :class:`HealthEvent` on each
``tick()``. The :class:`IncidentTracker` groups emitted events by
fingerprint, opens incidents, and consults ``close_condition`` to know
when an open incident should resolve.

Most detectors subclass :class:`DetectorBase` and override only the
observe/tick hooks they care about; the base provides safe no-op
defaults for the rest.
"""

from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable

from syncfield.health.severity import Severity
from syncfield.health.types import Incident, WriterStats
from syncfield.types import HealthEvent, SampleEvent, SessionState


@runtime_checkable
class Detector(Protocol):
    name: str
    default_severity: Severity

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None: ...
    def observe_health(self, stream_id: str, event: HealthEvent) -> None: ...
    def observe_state(self, old: SessionState, new: SessionState) -> None: ...
    def observe_writer_stats(self, stream_id: str, stats: WriterStats) -> None: ...
    def observe_connection_state(self, stream_id: str, new_state: str, at_ns: int) -> None: ...
    def tick(self, now_ns: int) -> Iterator[HealthEvent]: ...
    def close_condition(self, incident: Incident, now_ns: int) -> bool: ...


class DetectorBase:
    """No-op defaults for every Detector hook.

    Subclasses set ``name`` and ``default_severity`` at the class level
    and override only the hooks that matter for their rule.
    """

    name: str
    default_severity: Severity

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Require each subclass chain to end at a concrete class that declares
        # these attrs. We walk the MRO up to (but not including) DetectorBase
        # and assert at least one class in that chain sets each attribute
        # as an own attribute (via __dict__). This prevents a grandchild from
        # silently inheriting a default it shouldn't while still allowing
        # legitimate intermediate base classes.
        for attr in ("name", "default_severity"):
            declared = any(
                attr in klass.__dict__
                for klass in cls.__mro__
                if klass is not DetectorBase and klass is not object
            )
            if not declared:
                raise TypeError(
                    f"Detector subclass {cls.__name__} must set class attribute '{attr}'"
                )

    def __new__(cls, *args: object, **kwargs: object) -> "DetectorBase":
        if cls is DetectorBase:
            raise TypeError("DetectorBase is abstract; subclass it")
        return super().__new__(cls)

    def observe_sample(self, stream_id: str, sample: SampleEvent) -> None:
        pass

    def observe_health(self, stream_id: str, event: HealthEvent) -> None:
        pass

    def observe_state(self, old: SessionState, new: SessionState) -> None:
        pass

    def observe_writer_stats(self, stream_id: str, stats: WriterStats) -> None:
        pass

    def observe_connection_state(self, stream_id: str, new_state: str, at_ns: int) -> None:
        pass

    def tick(self, now_ns: int) -> Iterator[HealthEvent]:
        return iter(())

    def close_condition(self, incident: Incident, now_ns: int) -> bool:
        # Conservative default: keep open. Subclasses override to close.
        return False
