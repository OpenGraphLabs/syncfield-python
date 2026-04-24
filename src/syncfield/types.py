"""Core data types for SyncField timestamp capture.

These types produce JSONL output compatible with the SyncField synchronization
pipeline (syncfield-app). The format matches the recorder's frame_timestamps.jsonl
schema, enabling seamless integration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Union

from syncfield.health.severity import Severity

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


@dataclass(frozen=True)
class RecordingAnchor:
    """Per-stream anchor info captured when recording is armed.

    Captures the common host ``armed_host_ns`` (shared by all streams in
    the session) together with the first recorded frame's ``(host_ts,
    device_ts)`` pair for this stream. Downstream sync tooling uses the
    difference ``first_frame_host_ns - armed_host_ns`` to estimate each
    adapter's observed pipeline latency and remove per-adapter bias when
    aligning streams.

    Attributes:
        armed_host_ns: Common host monotonic_ns captured by the
            orchestrator immediately before ``start_recording()`` is
            fanned out to streams. Identical across all streams in a
            single recording window.
        first_frame_host_ns: Host monotonic_ns at which this stream's
            first recorded frame arrived on the host.
        first_frame_device_ns: Optional device-clock timestamp of the
            first recorded frame. ``None`` for adapters without a
            device-side clock (UVC webcams, host audio, etc).
    """

    armed_host_ns: int
    first_frame_host_ns: int
    first_frame_device_ns: int | None = None

    def __post_init__(self) -> None:
        if self.first_frame_host_ns < self.armed_host_ns:
            raise ValueError(
                f"first_frame_host_ns must be >= armed_host_ns; "
                f"got armed={self.armed_host_ns}, first={self.first_frame_host_ns}"
            )

    @property
    def first_frame_latency_ns(self) -> int:
        """Observed latency from armed moment to first frame arrival."""
        return self.first_frame_host_ns - self.armed_host_ns

    def to_dict(self) -> dict[str, Any]:
        return {
            "armed_host_ns": self.armed_host_ns,
            "first_frame_host_ns": self.first_frame_host_ns,
            "first_frame_device_ns": self.first_frame_device_ns,
            "first_frame_latency_ns": self.first_frame_latency_ns,
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
        extras: Optional adapter-specific per-frame metadata (e.g.
            ``{"quest_native_ns": 1234567890}`` for clock-drift correction).
            Each key is serialised as a top-level field in the JSONL row,
            so downstream readers can pick it up without schema migration.
    """

    frame_number: int
    capture_ns: int
    clock_source: str = "host_monotonic"
    clock_domain: str = "local_host"
    uncertainty_ns: int = 5_000_000  # 5 ms
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "frame_number": self.frame_number,
            "capture_ns": self.capture_ns,
            "clock_source": self.clock_source,
            "clock_domain": self.clock_domain,
            "uncertainty_ns": self.uncertainty_ns,
        }
        if self.extras:
            out.update(self.extras)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrameTimestamp:
        known = {
            "frame_number", "capture_ns", "clock_source",
            "clock_domain", "uncertainty_ns",
        }
        return cls(
            frame_number=data["frame_number"],
            capture_ns=data["capture_ns"],
            clock_source=data.get("clock_source", "host_monotonic"),
            clock_domain=data.get("clock_domain", "local_host"),
            uncertainty_ns=data.get("uncertainty_ns", 5_000_000),
            extras={k: v for k, v in data.items() if k not in known},
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
        live_preview: True if the stream should route to a live video preview
            (rather than a standalone recorder panel). Defaults to True.
    """

    provides_audio_track: bool = False
    supports_precise_timestamps: bool = False
    is_removable: bool = False
    produces_file: bool = False
    target_hz: float | None = None
    live_preview: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "provides_audio_track": self.provides_audio_track,
            "supports_precise_timestamps": self.supports_precise_timestamps,
            "is_removable": self.is_removable,
            "produces_file": self.produces_file,
            "target_hz": self.target_hz,
            "live_preview": self.live_preview,
        }


class SessionState(Enum):
    """Lifecycle state of a SessionOrchestrator.

    A SyncField session walks a small state machine. The 0.2 release
    adds a ``CONNECTED`` state so devices can stream live preview data
    before the user hits Record — the viewer and CLI both sit in this
    state to show frames and sensor values. A brief ``COUNTDOWN`` state
    fires between Record-click and actual recording so the user sees a
    3 / 2 / 1 indicator and has time to flick a glance at the rig.

    Typical single-recording transitions::

        IDLE → CONNECTED → COUNTDOWN → RECORDING → STOPPING → CONNECTED …

    Calling ``disconnect()`` from ``CONNECTED`` returns the session to
    ``IDLE``. Applications that want the legacy one-shot behavior can
    still call ``start()`` straight from ``IDLE`` — the orchestrator
    auto-connects, records, and ``stop()`` runs to ``STOPPED`` at the
    end.
    """

    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    PREPARING = "preparing"
    COUNTDOWN = "countdown"
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
    """A stream reports a health observation.

    ``severity`` / ``source`` / ``fingerprint`` / ``data`` enable the
    incident-tracking layer in :mod:`syncfield.health` to group many raw
    events into a single Sentry-style Incident. Adapters that don't care
    can leave them at their safe defaults; the platform will fill them
    in before the event reaches the IncidentTracker.
    """

    stream_id: str
    kind: HealthEventKind
    at_ns: int
    detail: str | None = None
    severity: Severity = Severity.INFO
    source: str = "unknown"
    fingerprint: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "kind": self.kind.value,
            "at_ns": self.at_ns,
            "detail": self.detail,
            "severity": self.severity.value,
            "source": self.source,
            "fingerprint": self.fingerprint,
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class SampleEvent:
    """A stream reports a sample (timestamp + optional channels) to the orchestrator.

    ``clock_domain`` lets an adapter override the default host clock domain
    when the timestamp's *origin* is a remote device rather than the local
    monotonic clock (e.g. a Meta Quest streaming poses over WiFi). Leaving
    it ``None`` — the common case for on-host captures — makes the
    orchestrator stamp the host's id so all local streams share one domain.
    """

    stream_id: str
    frame_number: int
    capture_ns: int
    channels: dict[str, "ChannelValue"] | None = None
    uncertainty_ns: int = 5_000_000
    clock_domain: str | None = None


@dataclass
class FinalizationReport:
    """Result of stopping a single Stream.

    Attributes:
        stream_id: Stream that was finalized.
        status: One of ``"completed"``, ``"partial"``, ``"failed"``,
            ``"pending_aggregation"``. The ``"pending_aggregation"`` status
            indicates the stream finished its synchronous lifecycle but a
            background aggregation job is still required to land all artifacts
            on disk.
        frame_count: Number of samples/frames produced.
        file_path: Path to any file the stream wrote, or None.
        first_sample_at_ns: Monotonic ns of first sample, or None if empty.
        last_sample_at_ns: Monotonic ns of last sample, or None if empty.
        health_events: Health events observed during recording.
        error: Error message if status is ``"failed"``.
        jitter_p95_ns: 95th-percentile inter-frame interval (ns) during
            the recording window. None if fewer than 20 samples were
            collected, or for non-video streams.
        jitter_p99_ns: 99th-percentile inter-frame interval (ns) during
            the recording window. None if fewer than 20 samples were
            collected.
        recording_anchor: Intra-host sync anchor captured at the start
            of the recording window (common ``armed_host_ns`` plus this
            stream's first-frame timestamps). ``None`` for empty
            recordings or adapters that haven't opted in.
    """

    stream_id: str
    status: Literal["completed", "partial", "failed", "pending_aggregation"]
    frame_count: int
    file_path: Path | None
    first_sample_at_ns: int | None
    last_sample_at_ns: int | None
    health_events: list[HealthEvent]
    error: str | None
    jitter_p95_ns: int | None = None
    jitter_p99_ns: int | None = None
    incidents: list = field(default_factory=list)
    recording_anchor: RecordingAnchor | None = None


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


ChirpSource = Literal["hardware", "software_fallback", "silent"]
"""Provenance tag for a :class:`ChirpEmission` timestamp.

- ``"hardware"``: ``hardware_ns`` was derived from the audio backend's
  DAC presentation timestamp (e.g. PortAudio ``outputBufferDacTime``).
  This is the best timestamp SyncField can produce.
- ``"software_fallback"``: the backend was real but could not supply a
  DAC time, so ``hardware_ns`` is ``None`` and the caller should use
  ``software_ns`` instead. Precision floor rises to ~1 ms jitter.
- ``"silent"``: no audio was actually played (``SilentChirpPlayer``,
  chirp disabled, or headless machine with no audio path). Chirp-anchored
  sync cannot use this host as a shared acoustic reference.
"""

_VALID_CHIRP_SOURCES = frozenset({"hardware", "software_fallback", "silent"})


@dataclass(frozen=True)
class ChirpEmission:
    """Result of playing a sync chirp: when it actually hit the DAC.

    The SDK prefers ``hardware_ns`` — captured from the audio driver's
    DAC presentation timestamp — and falls back to ``software_ns``
    (sampled immediately before the play call) when the backend cannot
    supply one. The ``source`` field tags which timestamp is
    authoritative so the downstream sync core can decide how much
    precision to claim for this host's chirp anchor.

    Attributes:
        software_ns: ``time.monotonic_ns()`` sampled by the caller right
            before handing the chirp to the audio backend. Always present.
        hardware_ns: Monotonic nanosecond estimate of when the first
            chirp sample will actually be clocked out of the DAC.
            ``None`` when the backend does not expose a hardware
            presentation time (e.g. silent player, or PortAudio backends
            without DAC time on the current host).
        source: Provenance tag, see :data:`ChirpSource`.
    """

    software_ns: int
    hardware_ns: int | None
    source: ChirpSource

    def __post_init__(self) -> None:
        if self.source not in _VALID_CHIRP_SOURCES:
            raise ValueError(
                "ChirpEmission.source must be one of "
                f"{sorted(_VALID_CHIRP_SOURCES)}; got {self.source!r}"
            )

    @property
    def best_ns(self) -> int:
        """Return ``hardware_ns`` when present, else ``software_ns``.

        This is the value the orchestrator persists into
        ``sync_point.json`` as ``chirp_start_ns`` / ``chirp_stop_ns``.
        """
        return self.hardware_ns if self.hardware_ns is not None else self.software_ns

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "software_ns": self.software_ns,
            "source": self.source,
        }
        if self.hardware_ns is not None:
            d["hardware_ns"] = self.hardware_ns
        return d


@dataclass
class SessionReport:
    """Aggregated result of a completed session.

    Attributes:
        host_id: Host identifier.
        finalizations: Per-stream finalization reports.
        chirp_start_ns: Best-available monotonic ns when the start chirp
            reached the DAC (hardware if available, else software
            fallback). ``None`` if no chirp was played.
        chirp_stop_ns: Best-available monotonic ns for the stop chirp.
        chirp_start_source: Provenance of ``chirp_start_ns`` — one of
            ``"hardware"``, ``"software_fallback"``, ``"silent"``, or
            ``None`` when no chirp was played.
        chirp_stop_source: Provenance of ``chirp_stop_ns``.
        session_id: Multi-host session identifier (from the attached
            role config), ``None`` for single-host sessions.
        role: ``"leader"``, ``"follower"``, or ``None`` for single-host.
    """

    host_id: str
    finalizations: list[FinalizationReport]
    chirp_start_ns: int | None
    chirp_stop_ns: int | None
    chirp_start_source: str | None = None
    chirp_stop_source: str | None = None
    session_id: str | None = None
    role: str | None = None
