"""Stream SPI — the contract every capture source must satisfy.

A :class:`Stream` is the fundamental unit that
:class:`~syncfield.orchestrator.SessionOrchestrator` coordinates. Two layers
live here:

- **Protocol** (:class:`Stream`) — a ``typing.Protocol`` describing the
  required attributes and methods. Third-party adapters that already have
  their own inheritance tree can conform structurally.
- **Base class** (:class:`StreamBase`) — a convenience superclass that
  handles callback registration and the internal health-event buffer so
  concrete adapters only need to implement a small set of lifecycle
  methods.

Physical device identity
------------------------

Each stream optionally exposes a :attr:`Stream.device_key` — a stable
``(adapter_type, device_id)`` tuple that names the **physical hardware**
the stream is bound to. Two streams that target the same USB webcam,
the same BLE MAC, or the same OAK serial share one key. The
orchestrator uses it to reject duplicate hardware registration, and
the discovery modal uses it to show "already added" state for devices
the session already owns. Streams with no meaningful hardware identity
(e.g. :class:`JSONLFileStream` reading a user-owned file) return
``None`` and fall back to stream-id uniqueness only.

Lifecycle
---------

SyncField 0.2 follows the same 4-phase lifecycle as the egonaut lab
recorder::

    prepare() → connect() → start_recording() → stop_recording() → disconnect()
     (once)     (live preview)   (file writing)   (file close)     (device close)

Adapters that want the full flow override all four capture methods so
preview frames keep flowing while the orchestrator is in the ``CONNECTED``
state, then file writing kicks in atomically on ``start_recording``.
Simpler adapters can override just ``prepare`` / ``start`` / ``stop`` —
:class:`StreamBase` provides backward-compatible defaults that route the
new methods through the legacy trio, so existing code keeps working.

All reference adapters in :mod:`syncfield.adapters` inherit from
:class:`StreamBase`. A third-party adapter is free to either inherit or
implement the protocol from scratch; both paths are equally well supported.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Protocol, Tuple, runtime_checkable

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

#: Stable identifier for the physical device a stream is bound to.
#: First element is the adapter type (``"uvc_webcam"``, ``"oak_camera"``,
#: …), second element is the per-device identifier (OpenCV index, OAK
#: mxid, BLE MAC, …). ``None`` means the stream has no meaningful
#: hardware identity — it will be compared on stream-id only.
DeviceKey = Tuple[str, str]


@runtime_checkable
class Stream(Protocol):
    """Abstract contract for a capture source managed by SessionOrchestrator.

    Lifecycle:
        1. ``prepare()`` — acquire one-shot resources (permissions,
           handles). May be called multiple times and must be idempotent.
        2. ``connect()`` — open the device and begin live capture for
           preview. After ``connect()`` the adapter must expose enough
           state for the viewer to render something (``latest_frame`` for
           video, plot data for sensors) but **must not** write to disk
           yet.
        3. ``start_recording(session_clock)`` — begin writing the
           captured data to the session output directory. This call
           must be fast and atomic — any slow setup belongs in
           ``connect()`` or ``prepare()``. All streams in a session
           receive ``start_recording()`` inside the orchestrator's
           ``COUNTDOWN → RECORDING`` transition, then the start chirp
           plays.
        4. ``stop_recording()`` — stop writing and return a
           :class:`~syncfield.types.FinalizationReport`. Called after
           the orchestrator plays the stop chirp. The device may stay
           open afterwards (``CONNECTED`` state) so the user can start
           a new recording without re-opening hardware.
        5. ``disconnect()`` — close the device and release resources.
           Called when the session returns to ``IDLE``.

    Legacy flow:
        ``prepare() → start(session_clock) → stop()`` still works — the
        default :class:`StreamBase` implementations of the new methods
        route through the legacy trio so existing adapters keep running.
        Adapters that want live preview should override the new
        methods explicitly.

    Callbacks:
        - ``on_sample(callback)`` — register a function called on every
          sample. Samples should only flow during ``RECORDING``; the
          preview path uses adapter-specific state like ``latest_frame``.
        - ``on_health(callback)`` — register a function called on
          health events.

    Thread safety:
        Sample and health callbacks may be invoked from a background
        thread owned by the stream. Callback functions must therefore
        be thread-safe.
    """

    id: str
    kind: StreamKind
    capabilities: StreamCapabilities

    @property
    def device_key(self) -> Optional[DeviceKey]:
        """Stable identifier for the physical device, or ``None``.

        See :data:`DeviceKey` for the contract. Used by
        ``SessionOrchestrator.add`` to reject duplicate hardware
        registration and by the discovery modal to show "already
        added" state.
        """
        ...

    def prepare(self) -> None: ...
    def start(self, session_clock: SessionClock) -> None: ...
    def stop(self) -> FinalizationReport: ...
    # New 4-phase lifecycle methods. Adapters that don't override them
    # inherit backward-compatible defaults from StreamBase that route
    # through prepare/start/stop.
    def connect(self) -> None: ...
    def start_recording(self, session_clock: SessionClock) -> None: ...
    def stop_recording(self) -> FinalizationReport: ...
    def disconnect(self) -> None: ...
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

    @property
    def device_key(self) -> Optional[DeviceKey]:
        """Return the physical device identifier, or ``None``.

        Default: ``None``. Override in adapters that wrap real hardware
        so the orchestrator can reject duplicate registration and the
        discovery modal can mark already-owned devices as "added".
        See :data:`DeviceKey`.
        """
        return None

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
    # Lifecycle methods — subclasses override.
    # ------------------------------------------------------------------
    #
    # Two levels of API here:
    #
    # 1. *Legacy trio* (``prepare`` / ``start`` / ``stop``) — the
    #    original 0.1 SPI. Existing adapters that only override these
    #    still work: the 4-phase defaults below route the new methods
    #    through the legacy ones.
    #
    # 2. *4-phase lifecycle* (``connect`` / ``start_recording`` /
    #    ``stop_recording`` / ``disconnect``) — added in 0.2 to support
    #    live preview before recording, atomic file writing, and
    #    reopen-friendly stop semantics. Adapters that want live preview
    #    should override these four directly and leave the legacy trio
    #    as no-ops (or keep them as thin convenience wrappers).

    def prepare(self) -> None:
        """Acquire one-shot resources (permissions, handles).

        Default: no-op. Override if your adapter needs to check
        permissions or preload state before the device opens.
        """
        pass

    def start(self, session_clock: SessionClock) -> None:  # pragma: no cover
        """Legacy one-shot start — open the device and begin writing.

        Default: raise. Legacy adapters override this; new-style
        adapters override :meth:`connect` and :meth:`start_recording`
        instead and leave this method alone.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement legacy start(). "
            "Either override start() or override connect() + start_recording()."
        )

    def stop(self) -> FinalizationReport:  # pragma: no cover
        """Legacy one-shot stop — stop writing and close the device.

        Default: raise. Legacy adapters override this; new-style
        adapters override :meth:`stop_recording` and :meth:`disconnect`
        instead.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement legacy stop(). "
            "Either override stop() or override stop_recording() + disconnect()."
        )

    # ------------------------------------------------------------------
    # 4-phase lifecycle — new in 0.2
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the device and begin live preview capture.

        Called when the orchestrator transitions ``IDLE → CONNECTED``.
        Override to open hardware and spawn the capture loop — data
        should start flowing so the viewer can show a live preview,
        but **do not** write anything to disk yet.

        Default: no-op. Backward-compat legacy adapters run everything
        in ``start()`` which the orchestrator calls inside
        :meth:`start_recording`.
        """
        pass

    def start_recording(self, session_clock: SessionClock) -> None:
        """Begin writing captured data to the session output.

        Called atomically on every stream right after the countdown
        completes, **before** the start chirp plays. Must be fast —
        any slow setup (opening a VideoWriter, allocating buffers)
        belongs in :meth:`connect` or :meth:`prepare`, not here.

        Default: falls back to the legacy :meth:`start` so existing
        adapters keep working without changes. New adapters should
        override this and leave :meth:`start` alone.
        """
        self.start(session_clock)

    def stop_recording(self) -> FinalizationReport:
        """Stop writing and return a finalization report.

        Called after the orchestrator plays the stop chirp, so the
        chirp is guaranteed to appear in any recorded audio track.
        After this call the device is still connected — the user can
        start another recording without re-opening hardware.

        Default: falls back to the legacy :meth:`stop`.
        """
        return self.stop()

    def disconnect(self) -> None:
        """Close the device and release capture resources.

        Called when the orchestrator transitions ``CONNECTED → IDLE``
        (or during a partial-failure rollback on ``start()``). After
        ``disconnect()`` the adapter must not hold any OS handles.

        Default: no-op. Legacy adapters release resources inside
        :meth:`stop` instead.
        """
        pass
