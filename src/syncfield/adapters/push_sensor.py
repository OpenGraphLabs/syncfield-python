"""PushSensorStream — generic helper for callback/asyncio/external-thread sources.

SDK contracts for GUI consumers (see syncfield-sensor-onboarding-enhancements §5):

1. **on_connect callback is non-blocking.**  ``PushSensorStream`` MUST NOT
   synchronously wait on the callback the user supplied via ``on_connect=``.
   The SDK fires it in a background daemon thread; the calling thread (and
   the orchestrator's connect loop) return immediately.  If the user's
   callback itself blocks, that blocks ONLY the callback's background thread,
   NOT the stream lifecycle or the viewer's "Connecting…" indicator.

2. **Burst-aware capture_ns interpolation.**  When the user's producer reads
   N > 1 samples in a single USB/BLE tick, it MUST NOT pass the same
   ``capture_ns`` to all N ``push()`` calls.  Instead it SHOULD call
   :func:`burst_timestamps` to obtain per-sample host-monotonic timestamps
   spaced by ``dt = 1e9 / expected_hz`` nanoseconds, anchored so the *last*
   sample lands at the actual read instant.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

from syncfield.adapters._generic import _SensorWriteCore, _resolve_capabilities
from syncfield.clock import SessionClock
from syncfield.stream import DeviceKey, StreamBase
from syncfield.types import (
    ChannelValue, FinalizationReport, HealthEvent, HealthEventKind,
    SampleEvent, StreamCapabilities,
)


def burst_timestamps(n: int, *, anchor_ns: Optional[int] = None, expected_hz: float) -> List[int]:
    """Compute per-sample host-monotonic timestamps for a burst read.

    When a single USB/BLE read returns *n* samples that were collected at a
    known fixed rate, this helper distributes timestamps so the **last**
    sample lands at *anchor_ns* (or ``time.monotonic_ns()`` if omitted) and
    earlier samples step backward by ``1e9 / expected_hz`` nanoseconds.

    The SDK contract for ``PushSensorStream`` requires callers to use this
    helper (or equivalent arithmetic) rather than passing the same
    ``capture_ns`` to all ``push()`` calls in a burst.  Clustering N samples
    at one tick degrades timestamp quality at high rates (1 kHz+) and defeats
    the sync alignment that depends on per-sample spread.

    Args:
        n: Number of samples in the burst.  Must be >= 1.
        anchor_ns: Host monotonic nanosecond timestamp for the *last* sample
            in the burst.  Defaults to ``time.monotonic_ns()`` at call time.
        expected_hz: Expected sensor sample rate in Hz.

    Returns:
        A list of *n* integer nanosecond timestamps in ascending order, with
        ``timestamps[-1] == anchor_ns`` and adjacent deltas equal to
        ``round(1e9 / expected_hz)``.

    Example::

        ts = burst_timestamps(5, anchor_ns=recv_ns, expected_hz=1000.0)
        for i, (sample, capture_ns) in enumerate(zip(burst, ts)):
            stream.push(sample, capture_ns=capture_ns)
    """
    if n < 1:
        raise ValueError(f"burst_timestamps: n must be >= 1, got {n}")
    if expected_hz <= 0:
        raise ValueError(f"burst_timestamps: expected_hz must be > 0, got {expected_hz}")
    if anchor_ns is None:
        anchor_ns = time.monotonic_ns()
    dt_ns = round(1e9 / expected_hz)
    return [anchor_ns - (n - 1 - i) * dt_ns for i in range(n)]


class PushSensorStream(StreamBase):
    """Generic helper for sensors driven by user-owned producer threads.

    SDK contracts for GUI consumers (see syncfield-sensor-onboarding-enhancements §5):

    1. **on_connect callback is non-blocking.**  The SDK fires the callback in
       a background daemon thread so the orchestrator's connect loop MUST NOT
       be blocked even if the user's ``on_connect`` coroutine/function takes
       time to complete.  See :func:`burst_timestamps` for Contract 2.

    2. **Burst-aware capture_ns interpolation.**  Callers who receive N > 1
       samples per USB/BLE tick MUST distribute timestamps using
       :func:`burst_timestamps` rather than passing the same ``capture_ns``
       to every ``push()`` in a burst.
    """

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
        """Open the stream for pushing.

        **SDK contract — non-blocking on_connect:**  The user-supplied
        ``on_connect`` callback MUST NOT block the calling thread.  The
        SDK fires it in a background daemon thread so the orchestrator's
        connect loop (and the GUI's "Connecting…" indicator) proceed
        immediately regardless of how long the callback takes.
        """
        self._connected = True
        if self._on_connect is not None:
            t = threading.Thread(
                target=self._on_connect,
                args=(self,),
                name=f"push-sensor-on-connect-{self.id}",
                daemon=True,
            )
            t.start()

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
        device_ns: Optional[int] = None,
        frame_number: Optional[int] = None,
    ) -> None:
        """Emit one sample.

        Args:
            channels: Decoded sample as a flat ``dict[str, ChannelValue]``.
            capture_ns: Host monotonic nanosecond timestamp at which the
                sample arrived (or was synthesized for back-filled bursts).
                Used for ``SampleEvent.capture_ns`` and for the recording
                anchor's ``first_frame_host_ns``. Defaults to
                ``time.monotonic_ns()`` at call time.
            device_ns: Optional device-clock nanosecond timestamp from the
                sensor itself (e.g. a BLE IMU that includes its own clock
                in the payload). When provided, it is recorded into the
                first-frame :class:`RecordingAnchor` so downstream sync
                tooling can scrub host-arrival jitter using device-clock
                deltas — exactly the way camera adapters with hardware
                clocks (Oak, Oglo, MetaQuestCamera) already do. Pass
                ``None`` (the default) when the sensor has no device-side
                clock; the anchor's ``first_frame_device_ns`` will be
                ``None`` and downstream alignment falls back to host
                arrival latency only.
            frame_number: Override the running frame counter. Most callers
                should leave this as ``None`` so the stream auto-increments.
        """
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
                # ``device_ns`` is recorded into the recording anchor only
                # for the first frame of each window (``_observe_first_frame``
                # is idempotent). Pass ``None`` when the sensor has no
                # device-side clock — the anchor stores ``first_frame_
                # device_ns=None`` and downstream sync uses host arrival.
                self._observe_first_frame(capture_ns, device_ns)
                self._write_core.record_sample(capture_ns)
