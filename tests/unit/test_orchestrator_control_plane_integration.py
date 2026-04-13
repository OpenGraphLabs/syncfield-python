"""Verify SessionOrchestrator wires the control plane correctly.

These tests use httpx to hit the real running control plane. They do
NOT exercise the full session lifecycle (device connect/disconnect)
because we're exclusively testing the control-plane wiring.
"""

import time

import httpx
import pytest

import syncfield as sf
from tests.unit.conftest import FakeStream


def _ping(port: int, session_id: str, timeout_s: float = 2.0) -> int:
    deadline = time.monotonic() + timeout_s
    last_err = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(
                f"http://127.0.0.1:{port}/health",
                headers={"Authorization": f"Bearer {session_id}"},
                timeout=0.25,
            )
            return r.status_code
        except Exception as exc:
            last_err = exc
            time.sleep(0.05)
    raise RuntimeError(f"control plane unreachable after {timeout_s}s: {last_err}")


class TestControlPlaneSpinUp:
    def test_single_host_does_not_start_control_plane(self, tmp_path) -> None:
        session = sf.SessionOrchestrator(host_id="mac_a", output_dir=tmp_path)
        # No role → no control plane. The attribute shouldn't even exist
        # yet; accessing it would raise AttributeError.
        assert session._control_plane is None

    def test_leader_starts_control_plane_with_port_7878_or_fallback(
        self, tmp_path
    ) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(
                session_id="amber-tiger-042",
                control_plane_port=0,  # use OS-assigned for test stability
                keep_alive_after_stop_sec=0.5,
            ),
        )
        session.add(FakeStream("cam"))
        audio_stream = FakeStream("mic")
        audio_stream.kind = "audio"
        session.add(audio_stream)

        # Reach into the private start-sequence bootstrap so we don't
        # have to spin up advertiser/chirp for this test: the orchestrator
        # exposes a small hook for test integration (see Step 11.2).
        session._start_control_plane_only_for_tests()

        try:
            port = session._control_plane.actual_port
            assert port > 0
            assert _ping(port, "amber-tiger-042") == 200
        finally:
            session._stop_control_plane_only_for_tests()

    def test_control_plane_uses_bearer_token_equal_to_session_id(
        self, tmp_path
    ) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(
                session_id="amber-tiger-042",
                control_plane_port=0,
                keep_alive_after_stop_sec=0.5,
            ),
        )
        session.add(FakeStream("cam"))
        audio_stream = FakeStream("mic")
        audio_stream.kind = "audio"
        session.add(audio_stream)

        session._start_control_plane_only_for_tests()
        try:
            port = session._control_plane.actual_port
            # Wrong token → 401.
            r = httpx.get(
                f"http://127.0.0.1:{port}/health",
                headers={"Authorization": "Bearer nope"},
                timeout=1.0,
            )
            assert r.status_code == 401
            # Right token → 200.
            assert _ping(port, "amber-tiger-042") == 200
        finally:
            session._stop_control_plane_only_for_tests()

    def test_stop_control_plane_releases_resources(self, tmp_path) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(
                session_id="amber-tiger-042",
                control_plane_port=0,
                keep_alive_after_stop_sec=0.5,
            ),
        )
        session.add(FakeStream("cam"))
        audio_stream = FakeStream("mic")
        audio_stream.kind = "audio"
        session.add(audio_stream)

        session._start_control_plane_only_for_tests()
        port = session._control_plane.actual_port
        session._stop_control_plane_only_for_tests()

        # After teardown: connection attempts should fail.
        with pytest.raises(Exception):
            httpx.get(
                f"http://127.0.0.1:{port}/health",
                headers={"Authorization": "Bearer amber-tiger-042"},
                timeout=0.5,
            )
