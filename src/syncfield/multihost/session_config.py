"""SessionConfig — the typed, wire-compatible shape of what the leader distributes.

Kept as plain ``@dataclass`` (no Pydantic, no FastAPI) so the orchestrator
can build, compare, and serialize these without pulling control-plane deps.
The corresponding Pydantic model for the HTTP layer lives in
:mod:`syncfield.multihost.control_plane.schemas`; they share keys one-to-one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

from syncfield.types import ChirpSpec


@dataclass(frozen=True)
class SessionConfig:
    """Session-global config that the leader distributes to every follower.

    Kept as a plain ``@dataclass`` rather than Pydantic so single-host
    and orchestrator code can import it without pulling FastAPI /
    Pydantic. The HTTP-layer mirror
    (:class:`~syncfield.multihost.control_plane.schemas.SessionConfigRequest`)
    is the strict typed version for on-the-wire validation; this class
    is the in-process representation.
    """

    session_name: str
    start_chirp: ChirpSpec
    stop_chirp: ChirpSpec
    recording_mode: str = "standard"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_name": self.session_name,
            "start_chirp": self.start_chirp.to_dict(),
            "stop_chirp": self.stop_chirp.to_dict(),
            "recording_mode": self.recording_mode,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionConfig":
        return cls(
            session_name=data["session_name"],
            start_chirp=ChirpSpec(**data["start_chirp"]),
            stop_chirp=ChirpSpec(**data["stop_chirp"]),
            recording_mode=data.get("recording_mode", "standard"),
        )


def validate_config_against_local_capabilities(
    config: SessionConfig,
    *,
    has_audio_stream: bool,
    supported_audio_range_hz: Tuple[float, float],
) -> None:
    """Raise ``ValueError`` if the local host cannot honor the config.

    Called on followers when a POST /session/config lands, and on the
    leader before distribution as a last-line self-check. The error
    message is surfaced verbatim to the operator via
    :class:`~syncfield.multihost.errors.ClusterConfigMismatch`.
    """
    if not has_audio_stream:
        raise ValueError(
            "follower has no audio-capable stream; cannot capture the chirp"
        )

    low, high = supported_audio_range_hz
    for chirp_name, chirp in (
        ("start_chirp", config.start_chirp),
        ("stop_chirp", config.stop_chirp),
    ):
        if chirp.from_hz < low or chirp.from_hz > high:
            raise ValueError(
                f"{chirp_name}.from_hz={chirp.from_hz} out of this host's "
                f"audio range [{low}, {high}]"
            )
        if chirp.to_hz < low or chirp.to_hz > high:
            raise ValueError(
                f"{chirp_name}.to_hz={chirp.to_hz} out of this host's "
                f"audio range [{low}, {high}]"
            )
