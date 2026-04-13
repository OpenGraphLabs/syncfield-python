"""Tests for the multi-host cluster endpoints on the viewer server.

The viewer's ``server.py`` imports ``PIL`` at module load time for
MJPEG encoding — a dependency that ships with the ``viewer`` optional
extra. Since ``Pillow`` is widely available and present in the dev
install, the real module is used; no stub is needed. We dispatch the
cluster endpoints through FastAPI's :class:`TestClient` against a
fake orchestrator. The test covers the acceptance criteria:

- single-host orchestrators get HTTP 409 on every cluster endpoint
- ``GET /api/cluster/peers`` includes ``self`` + every non-self
  announcement returned by the injected browser snapshot
- ``GET /api/cluster/health`` aggregates a reachable peer's health +
  a second peer that refuses the connection, without aborting
- ``POST /api/cluster/start`` from a follower returns HTTP 409
- ``POST /api/cluster/collect`` returns HTTP 409 while the
  orchestrator is still in the ``RECORDING`` state

We deliberately don't exercise the full happy path on collect —
that lives in the orchestrator-level integration test suite.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from fastapi.testclient import TestClient

from syncfield.multihost.types import SessionAnnouncement
from syncfield.roles import FollowerRole, LeaderRole
from syncfield.types import SessionState
from syncfield.viewer.server import ViewerServer


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, sid: str) -> None:
        self.id = sid
        self.kind = "video"
        self._last_fps = 30.0
        self._frame_count = 100
        self._dropped_count = 0
        self._last_frame_ns = 123
        self._bytes_written = 4096


class _FakeLock:
    def __enter__(self) -> "_FakeLock":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class FakeOrchestrator:
    """Minimal SessionOrchestrator-like object for endpoint tests."""

    def __init__(
        self,
        *,
        host_id: str = "host_a",
        role: Any = None,
        state: SessionState = SessionState.CONNECTED,
        session_id: Optional[str] = "amber-tiger-042",
        applied_session_config: Any = None,
        streams: Optional[Dict[str, Any]] = None,
        observed_leader: Optional[SessionAnnouncement] = None,
    ) -> None:
        self._host_id = host_id
        self._role = role
        self._state = state
        self._session_id = session_id
        self._applied_session_config = applied_session_config
        self._streams: Dict[str, Any] = streams or {
            "cam_main": _FakeStream("cam_main")
        }
        self._observed_leader = observed_leader
        self._browser = None
        self._control_plane = None
        self._episode_dir_created = False
        self._lock = _FakeLock()
        self.output_dir = __import__("pathlib").Path("/tmp/does-not-exist")
        self.task: Optional[str] = None

    # Surface expected by the cluster endpoints ----------------------

    @property
    def host_id(self) -> str:
        return self._host_id

    @property
    def role(self) -> Any:
        return self._role

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def observed_leader(self) -> Optional[SessionAnnouncement]:
        return self._observed_leader

    @staticmethod
    def _follower_base_url(ann: SessionAnnouncement) -> str:
        address = ann.resolved_address or "127.0.0.1"
        return f"http://{address}:{ann.control_plane_port}"

    def collect_from_followers(self, *args: Any, **kw: Any) -> Dict[str, Any]:
        return {"hosts": []}


class FakePoller:
    """Stand-in for SessionPoller — the cluster endpoints never touch it."""

    def get_snapshot(self) -> Any:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ann(
    host_id: str,
    *,
    status: str = "recording",
    port: int = 7878,
    address: str = "10.0.0.2",
    session_id: str = "amber-tiger-042",
) -> SessionAnnouncement:
    return SessionAnnouncement(
        session_id=session_id,
        host_id=host_id,
        status=status,  # type: ignore[arg-type]
        sdk_version="0.2.0",
        chirp_enabled=True,
        control_plane_port=port,
        resolved_address=address,
    )


def _build_client(orch: FakeOrchestrator) -> TestClient:
    server = ViewerServer(session=orch, poller=FakePoller())
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_peers_endpoint_409_when_role_is_none() -> None:
    orch = FakeOrchestrator(role=None, session_id=None)
    client = _build_client(orch)
    r = client.get("/api/cluster/peers")
    assert r.status_code == 409
    assert r.json()["detail"] == "multi-host role not configured"


def test_config_endpoint_409_when_role_is_none() -> None:
    orch = FakeOrchestrator(role=None, session_id=None)
    client = _build_client(orch)
    r = client.get("/api/cluster/config")
    assert r.status_code == 409


def test_peers_includes_self_and_mdns_peers(monkeypatch: Any) -> None:
    orch = FakeOrchestrator(
        host_id="mac_a",
        role=LeaderRole(session_id="amber-tiger-042"),
        state=SessionState.RECORDING,
    )
    peer = _make_ann("mac_b", port=7879, address="10.0.0.3")

    def _fake_snapshot(self: ViewerServer) -> List[Any]:
        return [peer]

    monkeypatch.setattr(
        ViewerServer,
        "_snapshot_peer_announcements",
        _fake_snapshot,
        raising=True,
    )

    client = _build_client(orch)
    r = client.get("/api/cluster/peers")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == "amber-tiger-042"
    assert body["self_host_id"] == "mac_a"
    assert body["self_role"] == "leader"
    host_ids = [p["host_id"] for p in body["peers"]]
    assert host_ids == ["mac_a", "mac_b"]
    self_entry = body["peers"][0]
    peer_entry = body["peers"][1]
    assert self_entry["is_self"] is True
    assert self_entry["reachable"] is True
    assert peer_entry["is_self"] is False
    assert peer_entry["reachable"] is None
    assert peer_entry["role"] == "follower"
    assert peer_entry["control_plane_port"] == 7879


def test_health_mixes_reachable_and_unreachable_peers(monkeypatch: Any) -> None:
    orch = FakeOrchestrator(
        host_id="mac_a",
        role=LeaderRole(session_id="amber-tiger-042"),
        state=SessionState.RECORDING,
    )
    ok_peer = _make_ann("mac_b", port=7879, address="10.0.0.3")
    bad_peer = _make_ann("mac_c", port=7880, address="10.0.0.4")

    def _fake_snapshot(self: ViewerServer) -> List[Any]:
        return [ok_peer, bad_peer]

    monkeypatch.setattr(
        ViewerServer,
        "_snapshot_peer_announcements",
        _fake_snapshot,
        raising=True,
    )

    # Swap httpx.AsyncClient for a stub that answers the good peer and
    # raises on the bad one. We stub the async-context-manager pattern
    # the code uses: ``async with httpx.AsyncClient(...) as client``.
    import httpx

    class _FakeResp:
        def __init__(self, status_code: int, payload: Dict[str, Any]) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> Dict[str, Any]:
            return self._payload

    class _FakeClient:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def get(
            self, url: str, headers: Optional[Dict[str, str]] = None,
        ) -> _FakeResp:
            if "10.0.0.3" in url:
                if url.endswith("/health"):
                    return _FakeResp(200, {
                        "host_id": "mac_b",
                        "role": "follower",
                        "state": "recording",
                        "sdk_version": "0.2.0",
                        "uptime_s": 10.0,
                    })
                return _FakeResp(200, {"streams": []})
            raise RuntimeError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    client = _build_client(orch)
    r = client.get("/api/cluster/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == "amber-tiger-042"
    hosts_by_id = {h["host_id"]: h for h in body["hosts"]}
    assert set(hosts_by_id) == {"mac_a", "mac_b", "mac_c"}
    assert hosts_by_id["mac_a"]["is_self"] is True
    assert hosts_by_id["mac_a"]["status"] == "ok"
    assert hosts_by_id["mac_b"]["status"] == "ok"
    assert hosts_by_id["mac_b"]["health"]["host_id"] == "mac_b"
    assert hosts_by_id["mac_c"]["status"] == "unreachable"
    assert "connection refused" in hosts_by_id["mac_c"]["error"]


def test_cluster_start_requires_leader() -> None:
    orch = FakeOrchestrator(
        host_id="mac_b",
        role=FollowerRole(session_id="amber-tiger-042"),
        state=SessionState.CONNECTED,
    )
    client = _build_client(orch)
    r = client.post("/api/cluster/start")
    assert r.status_code == 409
    assert "LeaderRole" in r.json()["detail"]


def test_cluster_stop_requires_leader() -> None:
    orch = FakeOrchestrator(
        host_id="mac_b",
        role=FollowerRole(session_id="amber-tiger-042"),
        state=SessionState.CONNECTED,
    )
    client = _build_client(orch)
    r = client.post("/api/cluster/stop")
    assert r.status_code == 409


def test_cluster_collect_409_while_recording() -> None:
    orch = FakeOrchestrator(
        host_id="mac_a",
        role=LeaderRole(session_id="amber-tiger-042"),
        state=SessionState.RECORDING,
    )
    client = _build_client(orch)
    r = client.post("/api/cluster/collect")
    assert r.status_code == 409
    assert r.json()["detail"] == "session must be stopped before collecting"


def test_cluster_collect_leader_only() -> None:
    orch = FakeOrchestrator(
        host_id="mac_b",
        role=FollowerRole(session_id="amber-tiger-042"),
        state=SessionState.STOPPED,
    )
    client = _build_client(orch)
    r = client.post("/api/cluster/collect")
    assert r.status_code == 409
    assert "LeaderRole" in r.json()["detail"]
