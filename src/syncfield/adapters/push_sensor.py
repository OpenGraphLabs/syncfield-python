"""PushSensorStream — generic helper for callback/asyncio/external-thread sources."""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from syncfield.adapters._generic import _SensorWriteCore, _resolve_capabilities
from syncfield.clock import SessionClock
from syncfield.stream import DeviceKey, StreamBase
from syncfield.types import (
    ChannelValue, FinalizationReport, HealthEvent, HealthEventKind,
    SampleEvent, StreamCapabilities,
)


class PushSensorStream(StreamBase):
    """Generic helper for sensors driven by user-owned producer threads."""

    def __init__(
        self,
        id: str,
        *,
        on_connect: Optional[Callable[["PushSensorStream"], None]] = None,
        on_disconnect: Optional[Callable[["PushSensorStream"], None]] = None,
        device_key: Optional[DeviceKey] = None,
        capabilities: Optional[StreamCapabilities] = None,
    ) -> None:
        super().__init__(
            id=id,
            kind="sensor",
            capabilities=_resolve_capabilities(capabilities, precise=False),
        )
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._device_key = device_key
        self._write_core = _SensorWriteCore(id)
        self._push_lock = threading.Lock()
        self._connected = False
        self._writing = False

    @property
    def device_key(self) -> Optional[DeviceKey]:
        return self._device_key

    # ------------------------------------------------------------------
    # 4-phase lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self._connected = True
        if self._on_connect is not None:
            self._on_connect(self)

    def start_recording(self, session_clock: SessionClock) -> None:
        self._begin_recording_window(session_clock)
        self._write_core.reset_recording_stats()
        self._writing = True

    def stop_recording(self) -> FinalizationReport:
        self._writing = False
        frame_count = self._write_core.recorded_count
        first_at = self._write_core.first_sample_at_ns
        last_at = self._write_core.last_sample_at_ns
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=frame_count,
            file_path=None,
            first_sample_at_ns=first_at,
            last_sample_at_ns=last_at,
            health_events=list(self._collected_health),
            error=None,
            recording_anchor=self._recording_anchor(),
        )

    def disconnect(self) -> None:
        if self._on_disconnect is not None:
            try:
                self._on_disconnect(self)
            except Exception as exc:
                self._emit_health(HealthEvent(
                    self.id, HealthEventKind.WARNING,
                    time.monotonic_ns(), f"on_disconnect raised: {exc}",
                ))
        self._connected = False

    # ------------------------------------------------------------------
    # Push API
    # ------------------------------------------------------------------

    def push(
        self,
        channels: dict[str, ChannelValue],
        *,
        capture_ns: Optional[int] = None,
        frame_number: Optional[int] = None,
    ) -> None:
        if not isinstance(channels, dict):
            raise TypeError(
                f"PushSensorStream.push: channels must be dict, "
                f"got {type(channels).__name__}"
            )
        if not self._connected:
            self._emit_health(HealthEvent(
                self.id, HealthEventKind.WARNING, time.monotonic_ns(),
                "push() called outside connect/disconnect; sample dropped",
            ))
            return
        if capture_ns is None:
            capture_ns = time.monotonic_ns()
        with self._push_lock:
            if frame_number is None:
                frame_number = self._write_core.next_frame_number()
            self._emit_sample(SampleEvent(
                stream_id=self.id,
                frame_number=frame_number,
                capture_ns=capture_ns,
                channels=channels,
            ))
            if self._writing:
                # Push sensors have no device clock — pass None for device_ns.
                self._observe_first_frame(capture_ns, None)
                self._write_core.record_sample(capture_ns)
