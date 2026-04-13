"""ControlPlaneServer — uvicorn lifecycle, port selection, keep-alive timer."""

import time

import httpx
import pytest

from syncfield.multihost.control_plane.server import ControlPlaneServer
from tests.unit.multihost.control_plane.test_routes import _FakeOrchestrator


def _wait_until_serving(port: int, session_id: str, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = httpx.get(
                f"http://127.0.0.1:{port}/health",
                headers={"Authorization": f"Bearer {session_id}"},
                timeout=0.25,
            )
            if r.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError):
            time.sleep(0.05)
    return False


class TestControlPlaneServerLifecycle:
    def test_starts_and_serves_health(self) -> None:
        orch = _FakeOrchestrator()
        server = ControlPlaneServer(orchestrator=orch, preferred_port=0)
        server.start()
        try:
            assert server.actual_port > 0
            assert _wait_until_serving(server.actual_port, orch.session_id)
        finally:
            server.stop()

    def test_stop_is_idempotent(self) -> None:
        orch = _FakeOrchestrator()
        server = ControlPlaneServer(orchestrator=orch, preferred_port=0)
        server.start()
        server.stop()
        server.stop()  # should not raise

    def test_stop_releases_port(self) -> None:
        import socket

        orch = _FakeOrchestrator()
        server = ControlPlaneServer(orchestrator=orch, preferred_port=0)
        server.start()
        released_port = server.actual_port
        server.stop()

        # After stop() the port must be available to bind.
        time.sleep(0.2)
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", released_port))
        finally:
            probe.close()

    def test_keep_alive_timer_eventually_stops_server(self) -> None:
        orch = _FakeOrchestrator()
        server = ControlPlaneServer(
            orchestrator=orch,
            preferred_port=0,
            keep_alive_after_stop_sec=0.5,
        )
        server.start()
        assert _wait_until_serving(server.actual_port, orch.session_id)

        server.arm_keep_alive_shutdown()
        # Within 1.5 s the server should have stopped itself.
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and server.is_running:
            time.sleep(0.05)
        assert not server.is_running

    def test_explicit_stop_preempts_keep_alive_timer(self) -> None:
        orch = _FakeOrchestrator()
        server = ControlPlaneServer(
            orchestrator=orch,
            preferred_port=0,
            keep_alive_after_stop_sec=10.0,  # long — we'll preempt
        )
        server.start()
        server.arm_keep_alive_shutdown()
        # Immediate explicit stop.
        server.stop()
        assert not server.is_running

    def test_bearer_auth_blocks_wrong_token(self) -> None:
        orch = _FakeOrchestrator(session_id="real-id")
        server = ControlPlaneServer(orchestrator=orch, preferred_port=0)
        server.start()
        try:
            assert _wait_until_serving(server.actual_port, "real-id")
            r = httpx.get(
                f"http://127.0.0.1:{server.actual_port}/health",
                headers={"Authorization": "Bearer wrong-id"},
                timeout=1.0,
            )
            assert r.status_code == 401
        finally:
            server.stop()

    def test_delete_while_keep_alive_timer_armed_leaves_timer_cancellable(self) -> None:
        """DELETE /session with an armed keep-alive timer: the DELETE
        hits the auth gate, the fake records the call, and an explicit
        .stop() afterwards cleanly cancels the still-armed keep-alive
        timer. End-to-end race coverage belongs in a later phase where
        the real adapter is wired."""
        import httpx

        orch = _FakeOrchestrator()
        server = ControlPlaneServer(
            orchestrator=orch,
            preferred_port=0,
            keep_alive_after_stop_sec=5.0,
        )
        server.start()
        try:
            assert _wait_until_serving(server.actual_port, orch.session_id)
            server.arm_keep_alive_shutdown()
            resp = httpx.delete(
                f"http://127.0.0.1:{server.actual_port}/session",
                headers={"Authorization": f"Bearer {orch.session_id}"},
                timeout=1.0,
            )
            assert resp.status_code == 200
            assert orch.delete_called == 1
            # Server still running (fake didn't actually shut it down).
            assert server.is_running
        finally:
            server.stop()
        # Cleanly stopped, keep-alive timer was cancelled.
        assert not server.is_running
