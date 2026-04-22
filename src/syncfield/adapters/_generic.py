"""Private internals shared by PollingSensorStream and PushSensorStream."""

from __future__ import annotations

import threading
from typing import Optional

from syncfield.types import StreamCapabilities


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
