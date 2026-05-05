"""PollingSensorStream — generic helper for sensors with a read() function.

SDK contract for GUI consumers (see syncfield-sensor-onboarding-enhancements §5):

3. **Transient transport hiccup auto-reopen.**  When an ``open`` callback is
   provided, ``PollingSensorStream`` MUST attempt up to
   :data:`~syncfield.adapters._generic.TRANSIENT_REOPEN_MAX_ATTEMPTS` reopens
   with exponential backoff before surfacing a stream-level error.  This is
   implemented via :func:`~syncfield.adapters._generic.retry_open`.
"""

from __future__ import annotations

import inspect
import threading
import time
from typing import Any, Callable, Literal, Optional

from syncfield.adapters._generic import (
    _SensorWriteCore,
    _resolve_capabilities,
    retry_open,
)
from syncfield.clock import SessionClock
from syncfield.stream import DeviceKey, StreamBase
from syncfield.types import (
    ChannelValue,
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    StreamCapabilities,
)


class PollingSensorStream(StreamBase):
    """Generic helper that polls a user read() function on a fixed hz.

    SDK contract for GUI consumers (see syncfield-sensor-onboarding-enhancements §5):

    3. **Transient transport hiccup auto-reopen.**  When an ``open`` callback
       is supplied, ``PollingSensorStream.connect()`` MUST attempt up to
       :data:`~syncfield.adapters._generic.TRANSIENT_REOPEN_MAX_ATTEMPTS` (5)
       reopens with exponential backoff (≤ 30 s total) before surfacing a
       stream-level error.  A single ``OSError`` or ``SerialException`` MUST
       NOT propagate immediately — the adapter retries automatically so that
       brief USB re-enumerations or connection blips are transparent to GUI
       users.
    """

    def __init__(
        self,
        id: str,
        *,
        read: Callable[..., dict[str, ChannelValue]],
        hz: float,
        open: Optional[Callable[[], Any]] = None,
        close: Optional[Callable[[Any], None]] = None,
        device_key: Optional[DeviceKey] = None,
        capabilities: Optional[StreamCapabilities] = None,
        on_read_error: Literal["drop", "stop"] = "drop",
    ) -> None:
        super().__init__(
            id=id,
            kind="sensor",
            capabilities=_resolve_capabilities(
                capabilities, precise=True, target_hz=float(hz)
            ),
        )
        self._validate_arity(read, expects_handle=open is not None)
        self._read = read
        self._open = open
        self._close = close
        self._hz = hz
        self._period = 1.0 / hz
        self._on_read_error = on_read_error
        self._device_key = device_key

        self._write_core = _SensorWriteCore(id)
        self._handle: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._writing = False

    @staticmethod
    def _validate_arity(
        read: Callable[..., dict[str, ChannelValue]],
        *,
        expects_handle: bool,
    ) -> None:
        sig = inspect.signature(read)
        n_params = len(
            [p for p in sig.parameters.values()
             if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        )
        if expects_handle and n_params != 1:
            raise TypeError(
                f"read must accept exactly 1 argument when open is provided "
                f"(got {n_params})"
            )
        if not expects_handle and n_params != 0:
            raise TypeError(
                f"read must accept 0 arguments when open is not provided "
                f"(got {n_params})"
            )

    @property
    def device_key(self) -> Optional[DeviceKey]:
        return self._device_key

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------

    def _capture_once(self) -> bool:
        """One iteration of the capture loop. Returns False to halt the loop."""
        loop_start = time.monotonic()
        try:
            result = self._read(self._handle) if self._open else self._read()
        except Exception as exc:
            self._emit_health(HealthEvent(
                self.id, HealthEventKind.ERROR,
                time.monotonic_ns(), str(exc),
            ))
            if self._on_read_error == "stop":
                return False
            time.sleep(self._period)
            return True

        # ``read()`` may return either:
        #   - ``dict[str, ChannelValue]`` (legacy / no device clock), or
        #   - ``(dict[str, ChannelValue], device_ns: int | None)`` for
        #     sensors that read out a device-side clock alongside the
        #     payload (e.g. an SPI register that exposes the sensor's
        #     internal timer). The two-tuple form lets the recording
        #     anchor capture device_ns the same way camera adapters do
        #     without breaking existing single-return callers.
        device_ns: Optional[int] = None
        if isinstance(result, tuple) and len(result) == 2:
            channels, device_ns = result
        else:
            channels = result

        if not isinstance(channels, dict):
            self._emit_health(HealthEvent(
                self.id, HealthEventKind.ERROR,
                time.monotonic_ns(),
                f"read() returned {type(channels).__name__}, expected dict "
                f"or (dict, device_ns)",
            ))
            if self._on_read_error == "stop":
                return False
            time.sleep(self._period)
            return True

        if device_ns is not None and not isinstance(device_ns, int):
            self._emit_health(HealthEvent(
                self.id, HealthEventKind.WARNING,
                time.monotonic_ns(),
                f"read() returned non-int device_ns "
                f"({type(device_ns).__name__}); ignored for this sample",
            ))
            device_ns = None

        capture_ns = time.monotonic_ns()
        frame_number = self._write_core.next_frame_number()
        self._emit_sample(SampleEvent(
            stream_id=self.id,
            frame_number=frame_number,
            capture_ns=capture_ns,
            channels=channels,
            device_ns=device_ns,
        ))

        if self._writing:
            # ``device_ns`` is also recorded into the recording anchor on
            # the first frame of each window (``_observe_first_frame`` is
            # idempotent). Per-sample propagation happens above via
            # SampleEvent.device_ns.
            self._observe_first_frame(capture_ns, device_ns)
            self._write_core.record_sample(capture_ns)

        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, self._period - elapsed))
        return True

    def _capture_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._capture_once():
                return

    # ------------------------------------------------------------------
    # 4-phase lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the transport and start the polling loop.

        **SDK contract — transient transport reopen (Contract 3):**
        When an ``open`` callback was provided, this method MUST retry
        the open up to :data:`~syncfield.adapters._generic.TRANSIENT_REOPEN_MAX_ATTEMPTS`
        times with exponential backoff before propagating an exception.
        """
        if self._open is not None:
            self._handle = retry_open(
                self._open,
                stream_id=self.id,
            )
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop,
            name=f"polling-sensor-{self.id}",
            daemon=True,
        )
        self._thread.start()

    def start_recording(self, session_clock: SessionClock) -> None:
        self._begin_recording_window(session_clock)
        self._write_core.reset_recording_stats()
        self._writing = True

    def stop_recording(self) -> FinalizationReport:
        self._writing = False
        frame_count = self._write_core.recorded_count
        first_ns = self._write_core.first_sample_at_ns
        last_ns = self._write_core.last_sample_at_ns
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=frame_count,
            file_path=None,
            first_sample_at_ns=first_ns,
            last_sample_at_ns=last_ns,
            health_events=list(self._collected_health),
            error=None,
            recording_anchor=self._recording_anchor(),
        )

    def disconnect(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                self._emit_health(HealthEvent(
                    self.id, HealthEventKind.WARNING,
                    time.monotonic_ns(),
                    "capture thread did not exit within 3s",
                ))
        if self._close is not None and self._handle is not None:
            try:
                self._close(self._handle)
            except Exception as exc:
                self._emit_health(HealthEvent(
                    self.id, HealthEventKind.WARNING,
                    time.monotonic_ns(), f"close failed: {exc}",
                ))
        self._handle = None
