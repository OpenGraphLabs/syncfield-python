"""Tests for the Starlette replay server (no real network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from syncfield.replay.loader import load_session
from syncfield.replay.server import build_app


@pytest.fixture
def client_with_synthetic(synthetic_session: Path) -> TestClient:
    manifest = load_session(synthetic_session)
    app = build_app(manifest)
    return TestClient(app)


@pytest.fixture
def client_with_synced(synced_session: Path) -> TestClient:
    manifest = load_session(synced_session)
    app = build_app(manifest)
    return TestClient(app)


def test_get_session_returns_manifest_json(
    client_with_synthetic: TestClient,
) -> None:
    response = client_with_synthetic.get("/api/session")
    assert response.status_code == 200
    body = response.json()
    assert body["host_id"] == "test_rig"
    assert {s["id"] for s in body["streams"]} == {"cam_ego", "wrist_imu"}


def test_get_sync_report_404_when_missing(
    client_with_synthetic: TestClient,
) -> None:
    response = client_with_synthetic.get("/api/sync-report")
    assert response.status_code == 404


def test_get_sync_report_returns_json_when_present(
    client_with_synced: TestClient,
) -> None:
    response = client_with_synced.get("/api/sync-report")
    assert response.status_code == 200
    assert response.json()["streams"]["cam_ego"]["quality"] == "excellent"


def test_get_media_serves_video_bytes(
    client_with_synthetic: TestClient, synthetic_session: Path,
) -> None:
    response = client_with_synthetic.get("/media/cam_ego")
    assert response.status_code == 200
    expected = (synthetic_session / "cam_ego.mp4").read_bytes()
    assert response.content == expected


def test_get_media_supports_range_request(
    client_with_synthetic: TestClient,
) -> None:
    response = client_with_synthetic.get(
        "/media/cam_ego", headers={"Range": "bytes=0-15"},
    )
    assert response.status_code == 206
    assert "content-range" in {k.lower() for k in response.headers}
    assert len(response.content) == 16


def test_get_media_unknown_stream_returns_404(
    client_with_synthetic: TestClient,
) -> None:
    response = client_with_synthetic.get("/media/no_such_stream")
    assert response.status_code == 404


def test_get_data_serves_jsonl(
    client_with_synthetic: TestClient,
) -> None:
    response = client_with_synthetic.get("/data/wrist_imu.jsonl")
    assert response.status_code == 200
    assert b"channels" in response.content


def test_get_data_path_traversal_rejected(
    client_with_synthetic: TestClient,
) -> None:
    response = client_with_synthetic.get("/data/..%2Fetc%2Fpasswd")
    assert response.status_code in (400, 404)


def test_get_root_serves_index_html(
    client_with_synthetic: TestClient,
) -> None:
    response = client_with_synthetic.get("/")
    assert response.status_code == 200
    assert b"<html" in response.content.lower() or b"<!doctype" in response.content.lower()
