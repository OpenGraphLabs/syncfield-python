"""Core data types for SyncField timestamp capture.

These types produce JSONL output compatible with the SyncField synchronization
pipeline (syncfield-app). The format matches the recorder's frame_timestamps.jsonl
schema, enabling seamless integration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Union

# Sensor channel value type.
# Leaf values are always numeric (float | int).
# Structure can be nested dicts or lists.
#
# Examples:
#   flat:   {"accel_x": 0.12, "accel_y": -9.8}
#   nested: {"joints": {"wrist": [0.1, 0.2, 0.3]}}
#   grid:   {"pressure": [[0.1, 0.2], [0.3, 0.4]]}
ChannelValue = Union[float, int, list, dict]


@dataclass(frozen=True)
class SyncPoint:
    """Reference time captured at recording start.

    All timestamps in a session are monotonic nanoseconds on the same host.
    The SyncPoint anchors that monotonic clock to wall-clock time for
    cross-host alignment scenarios.

    Attributes:
        monotonic_ns: ``time.monotonic_ns()`` at session start.
        wall_clock_ns: ``time.time_ns()`` at session start (UTC).
        host_id: Identifier for this capture host.
        timestamp_ms: Wall-clock milliseconds (human-readable).
        iso_datetime: ISO 8601 formatted datetime string.
    """

    monotonic_ns: int
    wall_clock_ns: int
    host_id: str
    timestamp_ms: int
    iso_datetime: str

    @classmethod
    def create_now(cls, host_id: str) -> SyncPoint:
        """Capture a sync point at the current moment."""
        mono = time.monotonic_ns()
        wall = time.time_ns()
        return cls(
            monotonic_ns=mono,
            wall_clock_ns=wall,
            host_id=host_id,
            timestamp_ms=wall // 1_000_000,
            iso_datetime=datetime.now().isoformat(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "monotonic_ns": self.monotonic_ns,
            "wall_clock_ns": self.wall_clock_ns,
            "host_id": self.host_id,
            "timestamp_ms": self.timestamp_ms,
            "iso_datetime": self.iso_datetime,
        }


@dataclass
class FrameTimestamp:
    """Single timestamp for one data packet (camera frame or sensor sample).

    Compatible with the SyncField recorder's ``frame_timestamps.jsonl`` schema
    and the syncfield-app ``FrameTimestamp`` dataclass.

    Attributes:
        frame_number: Sequential index (0-based) within this stream.
        capture_ns: ``time.monotonic_ns()`` captured immediately after I/O read.
        clock_source: Origin of the timestamp (always ``"host_monotonic"`` for SDK).
        clock_domain: Host identifier — must match across all streams on the same host.
        uncertainty_ns: Estimated timing uncertainty in nanoseconds.
    """

    frame_number: int
    capture_ns: int
    clock_source: str = "host_monotonic"
    clock_domain: str = "local_host"
    uncertainty_ns: int = 5_000_000  # 5 ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_number": self.frame_number,
            "capture_ns": self.capture_ns,
            "clock_source": self.clock_source,
            "clock_domain": self.clock_domain,
            "uncertainty_ns": self.uncertainty_ns,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrameTimestamp:
        return cls(
            frame_number=data["frame_number"],
            capture_ns=data["capture_ns"],
            clock_source=data.get("clock_source", "host_monotonic"),
            clock_domain=data.get("clock_domain", "local_host"),
            uncertainty_ns=data.get("uncertainty_ns", 5_000_000),
        )


@dataclass
class SensorSample:
    """Single sensor data sample with embedded timestamp.

    Produces JSONL compatible with the syncfield-app ``SensorSample`` schema.
    Combines timestamp and channel data in one record, written to
    ``{stream_id}.jsonl``.

    Attributes:
        frame_number: Sequential index (0-based) within this stream.
        capture_ns: ``time.monotonic_ns()`` captured at data arrival.
        channels: Sensor channel values (e.g. ``{"accel_x": 0.12}``).
        clock_source: Origin of the timestamp.
        clock_domain: Host identifier.
        uncertainty_ns: Estimated timing uncertainty in nanoseconds.
    """

    frame_number: int
    capture_ns: int
    channels: dict[str, ChannelValue]
    clock_source: str = "host_monotonic"
    clock_domain: str = "local_host"
    uncertainty_ns: int = 5_000_000  # 5 ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_number": self.frame_number,
            "capture_ns": self.capture_ns,
            "clock_source": self.clock_source,
            "clock_domain": self.clock_domain,
            "uncertainty_ns": self.uncertainty_ns,
            "channels": self.channels,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SensorSample:
        return cls(
            frame_number=data.get("frame_number", 0),
            capture_ns=data["capture_ns"],
            channels=data["channels"],
            clock_source=data.get("clock_source", "host_monotonic"),
            clock_domain=data.get("clock_domain", "local_host"),
            uncertainty_ns=data.get("uncertainty_ns", 5_000_000),
        )
