"""Pydantic model shapes for the control-plane JSON API."""

import pytest

from syncfield.multihost.control_plane.schemas import (
    HealthResponse,
    SessionConfigRequest,
    SessionConfigResponse,
    SessionStateResponse,
    StreamHealth,
    StreamsResponse,
)


class TestHealthResponse:
    def test_minimal_fields_serialize(self) -> None:
        r = HealthResponse(
            host_id="mac_a",
            role="leader",
            state="recording",
            sdk_version="0.2.0",
            uptime_s=12.5,
        )
        payload = r.model_dump()
        assert payload["host_id"] == "mac_a"
        assert payload["role"] == "leader"
        assert payload["state"] == "recording"
        assert payload["sdk_version"] == "0.2.0"
        assert payload["uptime_s"] == 12.5

    def test_role_accepts_none_for_single_host(self) -> None:
        # A /health call against a single-host session should never
        # happen (no server spun up), but the schema still tolerates
        # role=None so a future use case isn't blocked by the contract.
        r = HealthResponse(
            host_id="h",
            role=None,
            state="idle",
            sdk_version="0.2.0",
            uptime_s=0.0,
        )
        assert r.role is None


class TestStreamsResponse:
    def test_stream_health_fields(self) -> None:
        s = StreamHealth(
            id="cam_main",
            kind="video",
            fps=30.1,
            frames=912,
            dropped=2,
            last_frame_ns=1234567890,
            bytes_written=18_200_000,
        )
        payload = s.model_dump()
        assert payload["id"] == "cam_main"
        assert payload["fps"] == 30.1
        assert payload["dropped"] == 2

    def test_streams_response_wraps_list(self) -> None:
        r = StreamsResponse(streams=[])
        assert r.streams == []

        r2 = StreamsResponse(
            streams=[
                StreamHealth(
                    id="cam_main", kind="video",
                    fps=30.0, frames=900, dropped=0,
                    last_frame_ns=None, bytes_written=0,
                )
            ]
        )
        assert len(r2.streams) == 1
        assert r2.streams[0].id == "cam_main"


class TestSessionConfig:
    def test_config_request_round_trips_typed_chirps(self) -> None:
        # Phase 4: the config contract is strict and typed. The request
        # must carry both start/stop chirp specs; we verify the payload
        # round-trips through the model without losing fields.
        req = SessionConfigRequest(
            session_name="test_session",
            start_chirp={
                "from_hz": 400.0, "to_hz": 2500.0, "duration_ms": 500,
                "amplitude": 0.8, "envelope_ms": 15,
            },
            stop_chirp={
                "from_hz": 2500.0, "to_hz": 400.0, "duration_ms": 500,
                "amplitude": 0.8, "envelope_ms": 15,
            },
            recording_mode="high_quality_video",
        )
        payload = req.model_dump()
        assert payload["session_name"] == "test_session"
        assert payload["start_chirp"]["from_hz"] == 400.0
        assert payload["stop_chirp"]["to_hz"] == 400.0
        assert payload["recording_mode"] == "high_quality_video"

    def test_config_response_echoes_stored_config(self) -> None:
        resp = SessionConfigResponse(
            session_name="test_session",
            start_chirp={
                "from_hz": 400.0, "to_hz": 2500.0, "duration_ms": 500,
                "amplitude": 0.8, "envelope_ms": 15,
            },
            stop_chirp={
                "from_hz": 2500.0, "to_hz": 400.0, "duration_ms": 500,
                "amplitude": 0.8, "envelope_ms": 15,
            },
            recording_mode="high_quality_video",
        )
        payload = resp.model_dump()
        assert payload["session_name"] == "test_session"
        assert payload["start_chirp"]["from_hz"] == 400.0


class TestSessionStateResponse:
    def test_state_transition_result(self) -> None:
        r = SessionStateResponse(state="recording", detail="started via /session/start")
        assert r.state == "recording"
        assert r.detail == "started via /session/start"


class TestTightenedConfigSchemas:
    def test_chirp_spec_model_serializes(self) -> None:
        from syncfield.multihost.control_plane.schemas import ChirpSpecModel

        m = ChirpSpecModel(
            from_hz=400.0, to_hz=2500.0, duration_ms=500,
            amplitude=0.8, envelope_ms=15,
        )
        payload = m.model_dump()
        assert payload["from_hz"] == 400.0
        assert payload["amplitude"] == 0.8

    def test_session_config_request_typed_fields(self) -> None:
        from syncfield.multihost.control_plane.schemas import SessionConfigRequest

        req = SessionConfigRequest(
            session_name="lab_01",
            start_chirp={"from_hz": 400.0, "to_hz": 2500.0, "duration_ms": 500,
                         "amplitude": 0.8, "envelope_ms": 15},
            stop_chirp={"from_hz": 2500.0, "to_hz": 400.0, "duration_ms": 500,
                        "amplitude": 0.8, "envelope_ms": 15},
            recording_mode="standard",
        )
        # Coerced into typed ChirpSpecModel.
        assert req.start_chirp.from_hz == 400.0
        assert req.stop_chirp.to_hz == 400.0

    def test_session_config_request_rejects_missing_chirp(self) -> None:
        from pydantic import ValidationError
        from syncfield.multihost.control_plane.schemas import SessionConfigRequest

        with pytest.raises(ValidationError):
            SessionConfigRequest(session_name="x")  # missing start_chirp/stop_chirp

    def test_session_config_response_echoes_shape(self) -> None:
        from syncfield.multihost.control_plane.schemas import SessionConfigResponse

        r = SessionConfigResponse(
            session_name="lab_01",
            start_chirp={"from_hz": 400.0, "to_hz": 2500.0, "duration_ms": 500,
                         "amplitude": 0.8, "envelope_ms": 15},
            stop_chirp={"from_hz": 2500.0, "to_hz": 400.0, "duration_ms": 500,
                        "amplitude": 0.8, "envelope_ms": 15},
            recording_mode="standard",
        )
        payload = r.model_dump()
        assert payload["start_chirp"]["from_hz"] == 400.0

    def test_session_config_request_rejects_unknown_keys(self) -> None:
        from pydantic import ValidationError
        from syncfield.multihost.control_plane.schemas import SessionConfigRequest

        with pytest.raises(ValidationError):
            SessionConfigRequest(
                session_name="lab_01",
                start_chirp={"from_hz": 400.0, "to_hz": 2500.0, "duration_ms": 500,
                             "amplitude": 0.8, "envelope_ms": 15},
                stop_chirp={"from_hz": 2500.0, "to_hz": 400.0, "duration_ms": 500,
                            "amplitude": 0.8, "envelope_ms": 15},
                recording_mode="standard",
                unexpected_field_here="this should 422",
            )

    def test_chirp_spec_model_rejects_unknown_keys(self) -> None:
        from pydantic import ValidationError
        from syncfield.multihost.control_plane.schemas import ChirpSpecModel

        with pytest.raises(ValidationError):
            ChirpSpecModel(
                from_hz=400.0, to_hz=2500.0, duration_ms=500,
                amplitude=0.8, envelope_ms=15,
                bogus="nope",
            )
