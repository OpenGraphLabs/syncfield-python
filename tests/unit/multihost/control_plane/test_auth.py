"""Bearer-token auth for the control plane.

The token is the session's session_id. We test the FastAPI dependency
via a tiny throwaway app + TestClient so the assertions pin real
HTTP behavior (status codes, WWW-Authenticate headers) rather than
implementation details.
"""

from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from syncfield.multihost.control_plane.auth import verify_session_token


class _FakeOrch:
    def __init__(self, sid):
        self.session_id = sid


def _make_app(session_id: str) -> FastAPI:
    app = FastAPI()
    app.state.orchestrator = _FakeOrch(session_id)

    @app.get("/protected", dependencies=[Depends(verify_session_token)])
    def protected():
        return {"ok": True}

    return app


class TestVerifySessionToken:
    def test_missing_header_returns_401(self) -> None:
        client = TestClient(_make_app("amber-tiger-042"))
        resp = client.get("/protected")
        assert resp.status_code == 401
        assert "detail" in resp.json()

    def test_wrong_scheme_returns_401(self) -> None:
        client = TestClient(_make_app("amber-tiger-042"))
        resp = client.get("/protected", headers={"Authorization": "Basic abcdef"})
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self) -> None:
        client = TestClient(_make_app("amber-tiger-042"))
        resp = client.get(
            "/protected", headers={"Authorization": "Bearer wrong-id"}
        )
        assert resp.status_code == 401

    def test_correct_token_returns_200(self) -> None:
        client = TestClient(_make_app("amber-tiger-042"))
        resp = client.get(
            "/protected", headers={"Authorization": "Bearer amber-tiger-042"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_missing_orchestrator_returns_500(self) -> None:
        # Misconfiguration path: app.state.orchestrator not set → server
        # error (not 401), because this is an auth-contract violation
        # on our side, not the client's.
        app = FastAPI()

        @app.get("/protected", dependencies=[Depends(verify_session_token)])
        def protected():
            return {"ok": True}

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/protected", headers={"Authorization": "Bearer anything"}
        )
        assert resp.status_code == 500

    def test_pre_observation_follower_session_id_none_returns_503(self) -> None:
        # Orchestrator exists but session_id isn't known yet (auto-discover
        # follower in the brief window before observing the leader).
        app = FastAPI()

        class _PreObservationOrchestrator:
            session_id = None

        app.state.orchestrator = _PreObservationOrchestrator()

        @app.get("/protected", dependencies=[Depends(verify_session_token)])
        def protected():
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/protected", headers={"Authorization": "Bearer x"})
        assert resp.status_code == 503
        assert "Retry-After" in resp.headers
