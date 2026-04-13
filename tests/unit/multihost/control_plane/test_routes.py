"""Endpoint tests using FastAPI TestClient + a fake orchestrator."""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient

from syncfield.multihost.control_plane.routes import build_control_plane_app


@dataclass
class _FakeStreamMetrics:
    id: str
    kind: str = "video"
    fps: float = 30.0
    frames: int = 0
    dropped: int = 0
    last_frame_ns: Optional[int] = None
    bytes_written: int = 0


@dataclass
class _FakeOrchestrator:
    """Minimal shape that `routes.py` expects."""

    host_id: str = "mac_a"
    session_id: str = "amber-tiger-042"
    role_kind: Optional[str] = "leader"
    state_name: str = "recording"
    sdk_version: str = "0.2.0"
    has_audio_stream: bool = True
    supported_audio_range_hz: tuple = (20.0, 20_000.0)
    streams_metrics: List[_FakeStreamMetrics] = field(default_factory=list)
    stored_config: Optional[Dict[str, Any]] = None

    start_called: int = 0
    stop_called: int = 0
    delete_called: int = 0
    applied_config_on_orch: Optional[Any] = None

    def snapshot_stream_metrics(self) -> List[_FakeStreamMetrics]:
        return list(self.streams_metrics)

    def trigger_start(self) -> str:
        self.start_called += 1
        self.state_name = "recording"
        return self.state_name

    def trigger_stop(self) -> str:
        self.stop_called += 1
        self.state_name = "stopped"
        return self.state_name

    def trigger_control_plane_shutdown(self) -> None:
        self.delete_called += 1

    def apply_distributed_config(self, config) -> None:
        self.applied_config_on_orch = config


def _client_for(orch: _FakeOrchestrator, started_at_monotonic_s: float = 0.0) -> TestClient:
    app = build_control_plane_app(
        orchestrator=orch,
        started_at_monotonic_s=started_at_monotonic_s or time.monotonic(),
    )
    return TestClient(app)


AUTH = {"Authorization": "Bearer amber-tiger-042"}


class TestHealth:
    def test_returns_orchestrator_identity(self) -> None:
        orch = _FakeOrchestrator(host_id="mac_a", role_kind="leader", state_name="recording")
        client = _client_for(orch)

        resp = client.get("/health", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["host_id"] == "mac_a"
        assert body["role"] == "leader"
        assert body["state"] == "recording"
        assert body["sdk_version"] == "0.2.0"
        assert body["uptime_s"] >= 0.0

    def test_requires_bearer(self) -> None:
        orch = _FakeOrchestrator()
        client = _client_for(orch)
        assert client.get("/health").status_code == 401


class TestStreams:
    def test_empty_streams_list(self) -> None:
        orch = _FakeOrchestrator(streams_metrics=[])
        client = _client_for(orch)
        resp = client.get("/streams", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json() == {"streams": []}

    def test_populated_streams(self) -> None:
        orch = _FakeOrchestrator(streams_metrics=[
            _FakeStreamMetrics(id="cam_main", kind="video", fps=30.0, frames=900),
            _FakeStreamMetrics(id="mic", kind="audio", fps=0.0, frames=0, last_frame_ns=12345),
        ])
        client = _client_for(orch)
        resp = client.get("/streams", headers=AUTH)
        body = resp.json()
        ids = [s["id"] for s in body["streams"]]
        assert ids == ["cam_main", "mic"]
        assert body["streams"][1]["last_frame_ns"] == 12345


class TestSessionTriggers:
    def test_start_is_idempotent_and_returns_state(self) -> None:
        orch = _FakeOrchestrator(state_name="preparing")
        client = _client_for(orch)

        resp = client.post("/session/start", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["state"] == "recording"
        assert orch.start_called == 1

        # Idempotent: second call still returns 200 with state=recording.
        resp2 = client.post("/session/start", headers=AUTH)
        assert resp2.status_code == 200
        assert orch.start_called == 2

    def test_stop_transitions_state(self) -> None:
        orch = _FakeOrchestrator(state_name="recording")
        client = _client_for(orch)
        resp = client.post("/session/stop", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["state"] == "stopped"
        assert orch.stop_called == 1


class TestSessionConfig:
    def test_config_round_trip(self) -> None:
        orch = _FakeOrchestrator()
        client = _client_for(orch)

        config_payload = {
            "session_name": "lab_run_01",
            "start_chirp": {"from_hz": 400.0, "to_hz": 2500.0, "duration_ms": 500,
                            "amplitude": 0.8, "envelope_ms": 15},
            "stop_chirp": {"from_hz": 2500.0, "to_hz": 400.0, "duration_ms": 500,
                           "amplitude": 0.8, "envelope_ms": 15},
            "recording_mode": "standard",
        }
        post = client.post("/session/config", json=config_payload, headers=AUTH)
        assert post.status_code == 200
        assert post.json()["session_name"] == "lab_run_01"

        get = client.get("/session/config", headers=AUTH)
        assert get.status_code == 200
        assert get.json() == post.json()

    def test_get_before_post_returns_404(self) -> None:
        orch = _FakeOrchestrator()
        client = _client_for(orch)
        resp = client.get("/session/config", headers=AUTH)
        assert resp.status_code == 404


class TestConfigValidation:
    def test_valid_config_is_applied(self) -> None:
        orch = _FakeOrchestrator(has_audio_stream=True)
        client = _client_for(orch)

        payload = {
            "session_name": "lab_01",
            "start_chirp": {"from_hz": 400.0, "to_hz": 2500.0, "duration_ms": 500,
                            "amplitude": 0.8, "envelope_ms": 15},
            "stop_chirp": {"from_hz": 2500.0, "to_hz": 400.0, "duration_ms": 500,
                           "amplitude": 0.8, "envelope_ms": 15},
            "recording_mode": "standard",
        }
        r = client.post("/session/config", json=payload, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["session_name"] == "lab_01"
        assert body["start_chirp"]["from_hz"] == 400.0

        # GET returns same applied state.
        r2 = client.get("/session/config", headers=AUTH)
        assert r2.status_code == 200
        assert r2.json() == body

    def test_follower_without_audio_rejects_with_400(self) -> None:
        orch = _FakeOrchestrator(has_audio_stream=False)
        client = _client_for(orch)
        payload = {
            "session_name": "lab_01",
            "start_chirp": {"from_hz": 400.0, "to_hz": 2500.0, "duration_ms": 500,
                            "amplitude": 0.8, "envelope_ms": 15},
            "stop_chirp": {"from_hz": 2500.0, "to_hz": 400.0, "duration_ms": 500,
                           "amplitude": 0.8, "envelope_ms": 15},
        }
        r = client.post("/session/config", json=payload, headers=AUTH)
        assert r.status_code == 400
        assert "audio" in r.json()["detail"].lower()

    def test_chirp_out_of_audio_range_rejects_with_400(self) -> None:
        orch = _FakeOrchestrator(
            has_audio_stream=True,
            supported_audio_range_hz=(20.0, 20_000.0),
        )
        client = _client_for(orch)
        payload = {
            "session_name": "lab_01",
            "start_chirp": {"from_hz": 400.0, "to_hz": 30_000.0, "duration_ms": 500,
                            "amplitude": 0.8, "envelope_ms": 15},
            "stop_chirp": {"from_hz": 30_000.0, "to_hz": 400.0, "duration_ms": 500,
                           "amplitude": 0.8, "envelope_ms": 15},
        }
        r = client.post("/session/config", json=payload, headers=AUTH)
        assert r.status_code == 400
        assert "out of this host's audio range" in r.json()["detail"]

    def test_get_before_post_returns_404(self) -> None:
        orch = _FakeOrchestrator()
        client = _client_for(orch)
        r = client.get("/session/config", headers=AUTH)
        assert r.status_code == 404


class TestSessionDelete:
    def test_delete_triggers_shutdown(self) -> None:
        orch = _FakeOrchestrator()
        client = _client_for(orch)
        resp = client.delete("/session", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["state"] == "shutting_down"
        assert orch.delete_called == 1
