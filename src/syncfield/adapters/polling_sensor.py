"""PollingSensorStream — generic helper for sensors with a read() function."""

from __future__ import annotations

import inspect
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from syncfield.adapters._generic import (
    _SensorWriteCore,
    _resolve_capabilities,
)
from syncfield.clock import SessionClock
from syncfield.stream import DeviceKey, StreamBase
from syncfield.types import (
    ChannelValue,
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    SensorSample,
    StreamCapabilities,
)


class PollingSensorStream(StreamBase):
    """Generic helper that polls a user read() function on a fixed hz."""

    def __init__(
        self,
        id: str,
        *,
        read: Callable[..., dict[str, ChannelValue]],
        hz: float,
        output_dir: Path | str,
        open: Optional[Callable[[], Any]] = None,
        close: Optional[Callable[[Any], None]] = None,
        device_key: Optional[DeviceKey] = None,
        capabilities: Optional[StreamCapabilities] = None,
        on_read_error: Literal["drop", "stop"] = "drop",
    ) -> None:
        super().__init__(
            id=id,
            kind="sensor",
            capabilities=_resolve_capabilities(capabilities, precise=True),
        )
        self._validate_arity(read, expects_handle=open is not None)
        self._read = read
        self._open = open
        self._close = close
        self._hz = hz
        self._period = 1.0 / hz
        self._on_read_error = on_read_error
        self._device_key = device_key

        self._write_core = _SensorWriteCore(id, Path(output_dir))
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
            channels = self._read(self._handle) if self._open else self._read()
        except Exception as exc:
            self._emit_health(HealthEvent(
                self.id, HealthEventKind.ERROR,
                time.monotonic_ns(), str(exc),
            ))
            if self._on_read_error == "stop":
                return False
            time.sleep(self._period)
            return True

        if not isinstance(channels, dict):
            self._emit_health(HealthEvent(
                self.id, HealthEventKind.ERROR,
                time.monotonic_ns(),
                f"read() returned {type(channels).__name__}, expected dict",
            ))
            if self._on_read_error == "stop":
                return False
            time.sleep(self._period)
            return True

        capture_ns = time.monotonic_ns()
        frame_number = self._write_core.next_frame_number()
        self._emit_sample(SampleEvent(
            stream_id=self.id,
            frame_number=frame_number,
            capture_ns=capture_ns,
            channels=channels,
        ))

        if self._writing:
            try:
                self._write_core.write(SensorSample(
                    frame_number=frame_number,
                    capture_ns=capture_ns,
                    channels=channels,
                ))
            except Exception as exc:
                self._emit_health(HealthEvent(
                    self.id, HealthEventKind.ERROR,
                    time.monotonic_ns(),
                    f"sensor write failed: {exc}",
                ))

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
        if self._open is not None:
            self._handle = self._open()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop,
            name=f"polling-sensor-{self.id}",
            daemon=True,
        )
        self._thread.start()

    def start_recording(self, session_clock: SessionClock) -> None:
        self._write_core.open()
        self._writing = True

    def stop_recording(self) -> FinalizationReport:
        self._writing = False
        self._write_core.close()
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=self._write_core.frame_count,
            file_path=self._write_core.path,
            first_sample_at_ns=self._write_core.first_sample_at_ns,
            last_sample_at_ns=self._write_core.last_sample_at_ns,
            health_events=list(self._collected_health),
            error=None,
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
