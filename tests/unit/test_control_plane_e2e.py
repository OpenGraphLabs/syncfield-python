"""End-to-end: two orchestrators on localhost; follower-style cross-process HTTP.

Uses the real stack (FastAPI + uvicorn + httpx). No mocks beyond the
FakeStream contract we already lean on.
"""

import time

import httpx
import pytest

import syncfield as sf
from syncfield.multihost.control_plane import DEFAULT_KEEP_ALIVE_AFTER_STOP_SEC
from tests.unit.conftest import FakeStream

pytestmark = pytest.mark.slow


def _with_audio(session: "sf.SessionOrchestrator") -> None:
    stream = FakeStream("mic_for_chirp")
    stream.kind = "audio"
    session.add(stream)


def _probe_health(port: int, session_id: str, timeout_s: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last_err = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(
                f"http://127.0.0.1:{port}/health",
                headers={"Authorization": f"Bearer {session_id}"},
                timeout=0.5,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last_err = exc
            time.sleep(0.1)
    raise RuntimeError(f"health probe failed after {timeout_s}s: {last_err}")


class TestControlPlaneE2E:
    def test_leader_serves_health_endpoint(self, tmp_path) -> None:
        leader = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(
                session_id="amber-tiger-042",
                control_plane_port=0,
                keep_alive_after_stop_sec=1.0,
            ),
        )
        leader.add(FakeStream("cam"))
        _with_audio(leader)
        leader._start_control_plane_only_for_tests()
        try:
            port = leader._control_plane.actual_port
            body = _probe_health(port, "amber-tiger-042")
            assert body["host_id"] == "mac_a"
            assert body["role"] == "leader"
        finally:
            leader._stop_control_plane_only_for_tests()

    def test_session_config_round_trip(self, tmp_path) -> None:
        leader = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(
                session_id="amber-tiger-042",
                control_plane_port=0,
                keep_alive_after_stop_sec=1.0,
            ),
        )
        leader.add(FakeStream("cam"))
        _with_audio(leader)
        leader._start_control_plane_only_for_tests()
        try:
            port = leader._control_plane.actual_port
            auth = {"Authorization": "Bearer amber-tiger-042"}

            # POST config.
            payload = {
                "session_name": "lab_run_01",
                "start_chirp": {
                    "from_hz": 400.0, "to_hz": 2500.0, "duration_ms": 500,
                    "amplitude": 0.8, "envelope_ms": 15,
                },
                "stop_chirp": {
                    "from_hz": 2500.0, "to_hz": 400.0, "duration_ms": 500,
                    "amplitude": 0.8, "envelope_ms": 15,
                },
                "recording_mode": "standard",
            }
            r = httpx.post(
                f"http://127.0.0.1:{port}/session/config",
                json=payload,
                headers=auth,
                timeout=1.0,
            )
            assert r.status_code == 200
            assert r.json()["session_name"] == "lab_run_01"
            assert r.json()["start_chirp"]["from_hz"] == 400.0
            assert r.json()["stop_chirp"]["to_hz"] == 400.0
            assert r.json()["recording_mode"] == "standard"

            # GET echoes it back.
            r2 = httpx.get(
                f"http://127.0.0.1:{port}/session/config",
                headers=auth,
                timeout=1.0,
            )
            assert r2.status_code == 200
            assert r2.json()["session_name"] == "lab_run_01"
            assert r2.json()["start_chirp"]["from_hz"] == 400.0
        finally:
            leader._stop_control_plane_only_for_tests()

    def test_default_port_constant_matches_expected(self) -> None:
        # Sanity: the default keep-alive constant exposes a round number
        # so roles.py can refer to it by a literal without magic.
        assert DEFAULT_KEEP_ALIVE_AFTER_STOP_SEC == 600.0
