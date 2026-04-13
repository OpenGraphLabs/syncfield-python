"""Pydantic request/response models for the control plane.

Phase 4 tightens the ``SessionConfigRequest`` / ``SessionConfigResponse``
contract: the config payload is now strictly typed and mirrors
:class:`syncfield.multihost.session_config.SessionConfig`. Unknown keys
produce 422 instead of being silently stored — the endpoint is no longer
forward-compatible via extra fields, which is the right Phase-4 semantic
now that the cluster-wide config flow is live.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """Returned from ``GET /health`` — minimal liveness + identity."""

    host_id: str
    role: Optional[str] = Field(
        default=None,
        description="'leader' | 'follower' | None for single-host.",
    )
    state: str = Field(description="Current SessionState value.")
    sdk_version: str
    uptime_s: float = Field(description="Seconds since the control plane started.")


class StreamHealth(BaseModel):
    """Per-stream live metrics."""

    id: str
    kind: str = Field(description="'video' | 'audio' | 'sensor' | 'custom'.")
    fps: float = Field(description="Rolling frames-per-second over the last window.")
    frames: int = Field(description="Total frames since recording started.")
    dropped: int = Field(description="Frames dropped since recording started.")
    last_frame_ns: Optional[int] = Field(
        default=None,
        description="Monotonic ns of the most recent frame, or None if none yet.",
    )
    bytes_written: int = Field(
        default=0,
        description="Bytes flushed to disk for this stream.",
    )


class StreamsResponse(BaseModel):
    """Returned from ``GET /streams``."""

    streams: List[StreamHealth]


class ChirpSpecModel(BaseModel):
    """Pydantic mirror of :class:`syncfield.types.ChirpSpec` for the HTTP layer."""

    model_config = ConfigDict(extra="forbid")

    from_hz: float
    to_hz: float
    duration_ms: int
    amplitude: float
    envelope_ms: int


class SessionConfigRequest(BaseModel):
    """Body of ``POST /session/config`` — the leader's proposed cluster config."""

    model_config = ConfigDict(extra="forbid")

    session_name: str
    start_chirp: ChirpSpecModel
    stop_chirp: ChirpSpecModel
    recording_mode: str = "standard"


class SessionConfigResponse(BaseModel):
    """Body of ``GET /session/config`` and the 200 response of POST.

    Returns the applied (validated) config — NOT the raw submitted one.
    The two always match on the happy path; they differ only during
    defensive error reporting, which we do not expose in this phase.
    """

    model_config = ConfigDict(extra="forbid")

    session_name: str
    start_chirp: ChirpSpecModel
    stop_chirp: ChirpSpecModel
    recording_mode: str = "standard"


class SessionStateResponse(BaseModel):
    """Returned from ``POST /session/start``, ``POST /session/stop``, ``DELETE /session``."""

    state: str
    detail: Optional[str] = None


class FileManifestEntry(BaseModel):
    """One file in a host's post-session output tree."""
    model_config = ConfigDict(extra="forbid")

    path: str         # relative to host output dir, forward slashes
    size: int         # bytes
    sha256: str       # hex digest
    mtime_ns: int     # os.stat st_mtime_ns


class FileManifestResponse(BaseModel):
    """Returned by GET /files/manifest."""
    model_config = ConfigDict(extra="forbid")

    files: list[FileManifestEntry]


class DiscoveredDeviceResponse(BaseModel):
    """One device found by syncfield.discovery.scan() on a host."""
    model_config = ConfigDict(extra="forbid")

    adapter_type: str
    kind: str
    display_name: str
    description: str
    device_id: str
    accepts_output_dir: bool
    in_use: bool
    warnings: list[str]


class DiscoveryReportResponse(BaseModel):
    """Returned by GET /devices/discover — mirrors syncfield.discovery.DiscoveryReport."""
    model_config = ConfigDict(extra="forbid")

    devices: list[DiscoveredDeviceResponse]
    errors: dict[str, str]       # adapter_type -> error message
    timed_out: list[str]         # adapter_type values
    duration_s: float
