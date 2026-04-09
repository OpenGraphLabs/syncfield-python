"""Core data types for SyncField timestamp capture.

These types produce JSONL output compatible with the SyncField synchronization
pipeline (syncfield-app). The format matches the recorder's frame_timestamps.jsonl
schema, enabling seamless integration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Union

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


StreamKind = Literal["video", "audio", "sensor", "custom"]


@dataclass(frozen=True)
class StreamCapabilities:
    """What a Stream declares it can provide.

    Attributes:
        provides_audio_track: True if the stream records an audio track
            (used to determine chirp eligibility for inter-host sync).
        supports_precise_timestamps: True if per-sample timestamps are
            accurate to nanosecond resolution.
        is_removable: True if the underlying device may disconnect
            (wireless, USB unplug); the orchestrator treats it more defensively.
        produces_file: True if the stream writes a file (e.g. video) rather
            than an in-memory sample stream.
    """

    provides_audio_track: bool = False
    supports_precise_timestamps: bool = False
    is_removable: bool = False
    produces_file: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provides_audio_track": self.provides_audio_track,
            "supports_precise_timestamps": self.supports_precise_timestamps,
            "is_removable": self.is_removable,
            "produces_file": self.produces_file,
        }


class SessionState(Enum):
    """Lifecycle state of a SessionOrchestrator."""

    IDLE = "idle"
    PREPARING = "preparing"
    RECORDING = "recording"
    STOPPING = "stopping"
    STOPPED = "stopped"


class HealthEventKind(Enum):
    """Category of a health event reported by a Stream."""

    HEARTBEAT = "heartbeat"
    DROP = "drop"
    RECONNECT = "reconnect"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class HealthEvent:
    """A stream reports a health observation to the orchestrator.

    Attributes:
        stream_id: Stream that emitted the event.
        kind: Category of the event.
        at_ns: ``time.monotonic_ns()`` when the event was observed.
        detail: Optional free-form description.
    """

    stream_id: str
    kind: HealthEventKind
    at_ns: int
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "kind": self.kind.value,
            "at_ns": self.at_ns,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SampleEvent:
    """A stream reports a sample (timestamp + optional channels) to the orchestrator."""

    stream_id: str
    frame_number: int
    capture_ns: int
    channels: dict[str, "ChannelValue"] | None = None
    uncertainty_ns: int = 5_000_000


@dataclass
class FinalizationReport:
    """Result of stopping a single Stream.

    Attributes:
        stream_id: Stream that was finalized.
        status: One of ``"completed"``, ``"partial"``, ``"failed"``.
        frame_count: Number of samples/frames produced.
        file_path: Path to any file the stream wrote, or None.
        first_sample_at_ns: Monotonic ns of first sample, or None if empty.
        last_sample_at_ns: Monotonic ns of last sample, or None if empty.
        health_events: Health events observed during recording.
        error: Error message if status is ``"failed"``.
    """

    stream_id: str
    status: Literal["completed", "partial", "failed"]
    frame_count: int
    file_path: Path | None
    first_sample_at_ns: int | None
    last_sample_at_ns: int | None
    health_events: list[HealthEvent]
    error: str | None


@dataclass(frozen=True)
class ChirpSpec:
    """Specification for an audio sync chirp.

    Linear FM sweep from ``from_hz`` to ``to_hz`` over ``duration_ms``,
    with a cosine envelope of ``envelope_ms`` attack/release.

    Attributes:
        from_hz: Sweep start frequency (Hz).
        to_hz: Sweep end frequency (Hz).
        duration_ms: Total duration in milliseconds.
        amplitude: Peak amplitude in [0.0, 1.0].
        envelope_ms: Cosine fade in/out duration in milliseconds.
    """

    from_hz: float
    to_hz: float
    duration_ms: int
    amplitude: float
    envelope_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_hz": self.from_hz,
            "to_hz": self.to_hz,
            "duration_ms": self.duration_ms,
            "amplitude": self.amplitude,
            "envelope_ms": self.envelope_ms,
        }


@dataclass
class SessionReport:
    """Aggregated result of a completed session.

    Attributes:
        host_id: Host identifier.
        finalizations: Per-stream finalization reports.
        chirp_start_ns: Monotonic ns when start chirp was played (or None).
        chirp_stop_ns: Monotonic ns when stop chirp was played (or None).
    """

    host_id: str
    finalizations: list[FinalizationReport]
    chirp_start_ns: int | None
    chirp_stop_ns: int | None
