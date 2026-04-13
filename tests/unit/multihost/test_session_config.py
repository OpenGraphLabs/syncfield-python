"""SessionConfig construction + validation."""

from pathlib import Path

import pytest

from syncfield.multihost.session_config import (
    SessionConfig,
    validate_config_against_local_capabilities,
)
from syncfield.multihost.errors import ClusterConfigMismatch
from syncfield.types import ChirpSpec


def _chirp(from_hz=400.0, to_hz=2500.0, duration_ms=500) -> ChirpSpec:
    return ChirpSpec(
        from_hz=from_hz, to_hz=to_hz, duration_ms=duration_ms,
        amplitude=0.8, envelope_ms=15,
    )


class TestSessionConfigConstruction:
    def test_minimal_config_roundtrips(self) -> None:
        cfg = SessionConfig(
            session_name="lab_01",
            start_chirp=_chirp(),
            stop_chirp=_chirp(from_hz=2500.0, to_hz=400.0),
            recording_mode="standard",
        )
        d = cfg.to_dict()
        assert d["session_name"] == "lab_01"
        assert d["start_chirp"]["from_hz"] == 400.0
        assert d["recording_mode"] == "standard"

        cfg2 = SessionConfig.from_dict(d)
        assert cfg2 == cfg

    def test_recording_mode_defaults_to_standard(self) -> None:
        cfg = SessionConfig(
            session_name="x",
            start_chirp=_chirp(),
            stop_chirp=_chirp(),
        )
        assert cfg.recording_mode == "standard"


class TestValidateAgainstLocalCapabilities:
    def test_accepts_config_when_follower_has_audio_and_compatible_chirp(self) -> None:
        cfg = SessionConfig(
            session_name="lab_01",
            start_chirp=_chirp(from_hz=400.0, to_hz=2500.0),
            stop_chirp=_chirp(from_hz=2500.0, to_hz=400.0),
        )
        # Good path: follower has audio stream, chirp within supported range.
        validate_config_against_local_capabilities(
            cfg,
            has_audio_stream=True,
            supported_audio_range_hz=(20.0, 20_000.0),
        )

    def test_rejects_when_follower_has_no_audio_stream(self) -> None:
        cfg = SessionConfig(
            session_name="lab_01",
            start_chirp=_chirp(), stop_chirp=_chirp(),
        )
        with pytest.raises(ValueError, match="audio"):
            validate_config_against_local_capabilities(
                cfg, has_audio_stream=False, supported_audio_range_hz=(20.0, 20_000.0),
            )

    def test_rejects_when_chirp_exceeds_local_audio_range(self) -> None:
        # Leader wants 30 kHz sweep but local mic only supports 20 kHz.
        cfg = SessionConfig(
            session_name="x",
            start_chirp=_chirp(from_hz=400.0, to_hz=30_000.0),
            stop_chirp=_chirp(from_hz=30_000.0, to_hz=400.0),
        )
        with pytest.raises(ValueError, match="out of this host's audio range"):
            validate_config_against_local_capabilities(
                cfg, has_audio_stream=True, supported_audio_range_hz=(20.0, 20_000.0),
            )


class TestClusterConfigMismatchException:
    def test_message_includes_each_host_reason(self) -> None:
        exc = ClusterConfigMismatch({
            "mac_b": "no audio stream",
            "mac_c": "chirp out of range",
        })
        msg = str(exc)
        assert "mac_b" in msg
        assert "no audio stream" in msg
        assert "mac_c" in msg
        assert "chirp out of range" in msg

    def test_empty_rejections_is_programmer_error(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            ClusterConfigMismatch({})
