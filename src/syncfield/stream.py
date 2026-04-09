"""Stream SPI — the contract every capture source must satisfy.

A :class:`Stream` is the fundamental unit that
:class:`~syncfield.orchestrator.SessionOrchestrator` coordinates. Two layers
live here:

- **Protocol** (:class:`Stream`) — a ``typing.Protocol`` describing the
  required attributes and methods. Third-party adapters that already have
  their own inheritance tree can conform structurally.
- **Base class** (:class:`StreamBase`) — a convenience superclass that
  handles callback registration and the internal health-event buffer so
  concrete adapters only need to implement ``prepare``, ``start``, ``stop``.

All reference adapters in :mod:`syncfield.adapters` inherit from
:class:`StreamBase`. A third-party adapter is free to either inherit or
implement the protocol from scratch; both paths are equally well supported.
"""

from __future__ import annotations

from typing import Callable, List, Protocol, runtime_checkable

from syncfield.clock import SessionClock
from syncfield.types import (
    FinalizationReport,
    HealthEvent,
    SampleEvent,
    StreamCapabilities,
    StreamKind,
)


SampleCallback = Callable[[SampleEvent], None]
HealthCallback = Callable[[HealthEvent], None]


@runtime_checkable
class Stream(Protocol):
    """Abstract contract for a capture source managed by SessionOrchestrator.

    Lifecycle:
        1. ``prepare()`` — acquire resources, check permissions. May be called
           multiple times and must be idempotent.
        2. ``start(session_clock)`` — begin producing data, anchored to the
           session's shared monotonic clock.
        3. ``stop()`` — cease production and return a
           :class:`~syncfield.types.FinalizationReport`.

    Callbacks:
        - ``on_sample(callback)`` — register a function called on every sample.
        - ``on_health(callback)`` — register a function called on health events.

    Thread safety:
        Sample and health callbacks may be invoked from a background thread
        owned by the stream. Callback functions must therefore be thread-safe.
    """

    id: str
    kind: StreamKind
    capabilities: StreamCapabilities

    def prepare(self) -> None: ...
    def start(self, session_clock: SessionClock) -> None: ...
    def stop(self) -> FinalizationReport: ...
    def on_sample(self, callback: SampleCallback) -> None: ...
    def on_health(self, callback: HealthCallback) -> None: ...


class StreamBase:
    """Convenience base class that handles callback wiring.

    Concrete adapters inherit from this and implement ``prepare``, ``start``,
    ``stop``. Use ``self._emit_sample(ev)`` and ``self._emit_health(ev)`` from
    the data-producing code (e.g. a capture thread) to forward events to
    registered callbacks and to the internal health buffer.

    Args:
        id: Unique stream identifier within a session.
        kind: One of ``"video" | "audio" | "sensor" | "custom"``.
        capabilities: What the adapter declares it can provide.
    """

    def __init__(
        self,
        id: str,
        kind: StreamKind,
        capabilities: StreamCapabilities,
    ) -> None:
        self.id = id
        self.kind = kind
        self.capabilities = capabilities
        self._sample_callbacks: List[SampleCallback] = []
        self._health_callbacks: List[HealthCallback] = []
        self._collected_health: List[HealthEvent] = []

    def on_sample(self, callback: SampleCallback) -> None:
        """Register a callback invoked for every sample emitted by this stream."""
        self._sample_callbacks.append(callback)

    def on_health(self, callback: HealthCallback) -> None:
        """Register a callback invoked for every health event emitted by this stream."""
        self._health_callbacks.append(callback)

    def _emit_sample(self, event: SampleEvent) -> None:
        """Forward a sample event to all registered callbacks.

        Call from the stream's data-producing code (e.g. the frame loop).
        Callbacks run inline, so they must be cheap and non-blocking.
        """
        for cb in self._sample_callbacks:
            cb(event)

    def _emit_health(self, event: HealthEvent) -> None:
        """Forward a health event to callbacks and buffer it for finalization."""
        self._collected_health.append(event)
        for cb in self._health_callbacks:
            cb(event)

    # ------------------------------------------------------------------
    # Lifecycle methods — subclasses must override.
    # ------------------------------------------------------------------

    def prepare(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def start(self, session_clock: SessionClock) -> None:  # pragma: no cover
        raise NotImplementedError

    def stop(self) -> FinalizationReport:  # pragma: no cover
        raise NotImplementedError
