"""Pydantic request/response models for the control plane.

Kept deliberately thin in Phase 3 — Phase 4 will tighten the
``SessionConfigRequest`` contract with concrete field types once the
config-distribution flow is being built. Until then, the config
payload is intentionally loose so the endpoint can accept forward-
compatible additions without a breaking change.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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


class SessionConfigRequest(BaseModel):
    """Body of ``POST /session/config`` (Phase 4 will tighten)."""

    model_config = {"extra": "allow"}

    session_name: Optional[str] = None
    chirp_spec: Optional[Dict[str, Any]] = None
    recording_mode: Optional[str] = None


class SessionConfigResponse(BaseModel):
    """Body of ``GET /session/config`` and the 200 response of POST."""

    model_config = {"extra": "allow"}

    session_name: Optional[str] = None
    chirp_spec: Optional[Dict[str, Any]] = None
    recording_mode: Optional[str] = None


class SessionStateResponse(BaseModel):
    """Returned from ``POST /session/start``, ``POST /session/stop``, ``DELETE /session``."""

    state: str
    detail: Optional[str] = None
