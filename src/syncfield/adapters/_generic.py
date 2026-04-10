"""Private internals shared by PollingSensorStream and PushSensorStream."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from syncfield.types import SensorSample, StreamCapabilities
from syncfield.writer import SensorWriter


class _SensorWriteCore:
    """Owns the SensorWriter, frame counter, and timing trackers for a single sensor stream. Thread-safe: all public methods may be called from multiple threads."""

    def __init__(self, stream_id: str, output_dir: Path) -> None:
        self._stream_id = stream_id
        self._output_dir = Path(output_dir)
        self._writer: Optional[SensorWriter] = None
        self._lock = threading.Lock()
        self._frame_counter = 0
        self._first_at_ns: Optional[int] = None
        self._last_at_ns: Optional[int] = None

    @property
    def path(self) -> Path:
        return self._output_dir / f"{self._stream_id}.jsonl"

    @property
    def first_sample_at_ns(self) -> Optional[int]:
        return self._first_at_ns

    @property
    def last_sample_at_ns(self) -> Optional[int]:
        return self._last_at_ns

    @property
    def frame_count(self) -> int:
        return self._writer.count if self._writer is not None else 0

    def next_frame_number(self) -> int:
        with self._lock:
            n = self._frame_counter
            self._frame_counter += 1
            return n

    def open(self) -> None:
        with self._lock:
            if self._writer is not None:
                raise RuntimeError(
                    f"_SensorWriteCore for '{self._stream_id}' is already open"
                )
            self._output_dir.mkdir(parents=True, exist_ok=True)
            self._writer = SensorWriter(self._stream_id, self._output_dir)
            self._writer.open()

    def write(self, sample: SensorSample) -> None:
        with self._lock:
            if self._writer is None:
                raise RuntimeError(
                    f"_SensorWriteCore for '{self._stream_id}' is not open"
                )
            self._writer.write(sample)
            if self._first_at_ns is None:
                self._first_at_ns = sample.capture_ns
            self._last_at_ns = sample.capture_ns

    def close(self) -> None:
        with self._lock:
            if self._writer is not None:
                self._writer.close()
                self._writer = None


def _default_sensor_capabilities(*, precise: bool) -> StreamCapabilities:
    return StreamCapabilities(
        provides_audio_track=False,
        supports_precise_timestamps=precise,
        is_removable=False,
        produces_file=True,
    )


def _resolve_capabilities(
    user: Optional[StreamCapabilities],
    *,
    precise: bool,
) -> StreamCapabilities:
    if user is not None:
        return user
    return _default_sensor_capabilities(precise=precise)
