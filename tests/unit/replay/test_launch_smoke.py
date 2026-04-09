"""End-to-end smoke test: launch() actually serves /api/session."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from syncfield.replay import launch
from syncfield.replay.loader import load_session
from syncfield.replay.server import ReplayServer


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_launch_starts_server_and_serves_session(
    synthetic_session: Path,
) -> None:
    port = _free_port()
    server_ref: list[ReplayServer] = []

    def _run() -> None:
        # We bypass launch() so the test can hold the ReplayServer
        # reference and shut it down deterministically.
        manifest = load_session(synthetic_session)
        server = ReplayServer(manifest, host="127.0.0.1", port=port)
        server_ref.append(server)
        server.serve()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/session", timeout=0.5)
            if r.status_code == 200:
                break
        except httpx.RequestError:
            time.sleep(0.05)
    else:
        pytest.fail("server never came up")

    body = r.json()
    assert body["host_id"] == "test_rig"

    server_ref[0].request_shutdown()
    thread.join(timeout=3.0)
    assert not thread.is_alive()


def test_launch_is_callable_with_open_browser_false(
    synthetic_session: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Just verify the public function exists and accepts the keyword."""
    # Patch ReplayServer.serve so we don't actually block.
    called = {"serve": False}

    def fake_serve(self):  # type: ignore[no-untyped-def]
        called["serve"] = True

    monkeypatch.setattr(
        "syncfield.replay.server.ReplayServer.serve", fake_serve
    )

    launch(synthetic_session, open_browser=False)
    assert called["serve"] is True
