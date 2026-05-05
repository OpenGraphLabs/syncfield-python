"""Private internals shared by PollingSensorStream and PushSensorStream.

SDK contract for GUI consumers (see syncfield-sensor-onboarding-enhancements §5):

3. **Transient transport hiccup auto-reopen.**  Adapters that own a transport
   (serial port, BLE connection, USB pipe) MUST attempt up to
   :data:`TRANSIENT_REOPEN_MAX_ATTEMPTS` reopens with exponential backoff
   (total wall time ≤ :data:`TRANSIENT_REOPEN_MAX_WAIT_S` seconds) before
   surfacing a stream-level error.  Use :func:`retry_open` in the adapter's
   open / reconnect path to satisfy this contract automatically.
"""

from __future__ import annotations

import logging
import time
import threading
from typing import Callable, Optional, TypeVar

from syncfield.types import StreamCapabilities

logger = logging.getLogger(__name__)

#: Maximum number of reopen attempts before giving up (Contract 3).
TRANSIENT_REOPEN_MAX_ATTEMPTS: int = 5

#: Maximum total backoff wait across all retry attempts in seconds (Contract 3).
TRANSIENT_REOPEN_MAX_WAIT_S: float = 30.0

_T = TypeVar("_T")


def retry_open(
    open_fn: Callable[[], _T],
    *,
    max_attempts: int = TRANSIENT_REOPEN_MAX_ATTEMPTS,
    max_wait_s: float = TRANSIENT_REOPEN_MAX_WAIT_S,
    stream_id: str = "<unknown>",
) -> _T:
    """Call *open_fn* up to *max_attempts* times with exponential backoff.

    **SDK contract — transient transport reopen (Contract 3):**  Adapters
    that own a transport (serial, BLE, USB) MUST use this helper (or
    equivalent retry logic) in their open/reconnect path.  A single
    transient ``OSError`` or ``SerialException`` MUST NOT surface
    immediately as a stream-level failure.  The adapter MUST retry at
    least :data:`TRANSIENT_REOPEN_MAX_ATTEMPTS` times, sleeping 1 s,
    2 s, 4 s, … (capped so total wait ≤ *max_wait_s*) between attempts.

    Args:
        open_fn: Zero-argument callable that opens the transport and
            returns a handle (e.g. a ``serial.Serial`` instance).
            May raise any exception on transient failure.
        max_attempts: How many attempts before re-raising.  Defaults to
            :data:`TRANSIENT_REOPEN_MAX_ATTEMPTS` (5).
        max_wait_s: Hard cap on total sleep time across all retries.
            Defaults to :data:`TRANSIENT_REOPEN_MAX_WAIT_S` (30 s).
        stream_id: Stream identifier for log messages.

    Returns:
        The value returned by *open_fn* on success.

    Raises:
        Exception: The last exception raised by *open_fn* once all
            attempts are exhausted.
    """
    delay = 1.0
    last_exc: Optional[Exception] = None
    total_waited = 0.0
    for attempt in range(1, max_attempts + 1):
        try:
            return open_fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
            # Compute sleep duration capped by max_wait_s budget.
            # Even when the budget is exhausted we still retry up to
            # max_attempts times — max_wait_s limits sleep duration only,
            # not the number of attempts.
            remaining_budget = max_wait_s - total_waited
            wait = min(delay, max(remaining_budget, 0.0))
            logger.warning(
                "[%s] transient open error (attempt %d/%d): %s — retrying in %.1fs",
                stream_id, attempt, max_attempts, exc, wait,
            )
            if wait > 0:
                time.sleep(wait)
            total_waited += wait
            delay = delay * 2
    assert last_exc is not None
    raise last_exc


class _SensorWriteCore:
    """Frame counter and timing tracker for generic sensor helpers.

    Thread-safe: all public methods may be called from multiple threads.
    Disk persistence is handled by the SessionOrchestrator's on_sample
    callback — helpers only emit SampleEvents.
    """

    def __init__(self, stream_id: str) -> None:
        self._stream_id = stream_id
        self._lock = threading.Lock()
        self._frame_counter = 0
        self._recorded_count = 0
        self._first_at_ns: Optional[int] = None
        self._last_at_ns: Optional[int] = None

    @property
    def first_sample_at_ns(self) -> Optional[int]:
        return self._first_at_ns

    @property
    def last_sample_at_ns(self) -> Optional[int]:
        return self._last_at_ns

    @property
    def recorded_count(self) -> int:
        return self._recorded_count

    def next_frame_number(self) -> int:
        with self._lock:
            n = self._frame_counter
            self._frame_counter += 1
            return n

    def record_sample(self, capture_ns: int) -> None:
        """Track a recorded sample's timing. Called only during RECORDING."""
        with self._lock:
            self._recorded_count += 1
            if self._first_at_ns is None:
                self._first_at_ns = capture_ns
            self._last_at_ns = capture_ns

    def reset_recording_stats(self) -> None:
        """Reset recorded count and timing for a new recording cycle."""
        with self._lock:
            self._recorded_count = 0
            self._first_at_ns = None
            self._last_at_ns = None


def _default_sensor_capabilities(
    *,
    precise: bool,
    target_hz: Optional[float] = None,
) -> StreamCapabilities:
    return StreamCapabilities(
        provides_audio_track=False,
        supports_precise_timestamps=precise,
        is_removable=False,
        produces_file=False,  # orchestrator handles JSONL persistence
        target_hz=target_hz,
    )


def _resolve_capabilities(
    user: Optional[StreamCapabilities],
    *,
    precise: bool,
    target_hz: Optional[float] = None,
) -> StreamCapabilities:
    if user is not None:
        return user
    return _default_sensor_capabilities(precise=precise, target_hz=target_hz)
