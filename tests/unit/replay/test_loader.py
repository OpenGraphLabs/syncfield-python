"""Unit tests for syncfield.replay.loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncfield.replay.loader import ReplayManifest, load_session


def test_load_session_returns_manifest(synthetic_session: Path) -> None:
    manifest = load_session(synthetic_session)

    assert isinstance(manifest, ReplayManifest)
    assert manifest.session_dir == synthetic_session
    assert manifest.host_id == "test_rig"
    assert manifest.sync_point["host_id"] == "test_rig"
    assert manifest.sync_report is None
    assert manifest.has_frame_map is False


def test_load_session_finds_video_and_sensor_streams(
    synthetic_session: Path,
) -> None:
    manifest = load_session(synthetic_session)
    by_id = {s.id: s for s in manifest.streams}

    assert set(by_id) == {"cam_ego", "wrist_imu"}

    cam = by_id["cam_ego"]
    assert cam.kind == "video"
    assert cam.media_url == "/media/cam_ego"
    assert cam.media_path == synthetic_session / "cam_ego.mp4"
    assert cam.frame_count == 60

    imu = by_id["wrist_imu"]
    assert imu.kind == "sensor"
    assert imu.media_url is None
    assert imu.data_url == "/data/wrist_imu.jsonl"
    assert imu.frame_count == 600


def test_load_session_with_sync_report(synced_session: Path) -> None:
    manifest = load_session(synced_session)

    assert manifest.sync_report is not None
    assert manifest.sync_report["streams"]["cam_ego"]["quality"] == "excellent"


def test_load_session_missing_manifest_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_session(tmp_path / "does_not_exist")


def test_load_session_missing_sync_point_is_optional(
    synthetic_session: Path,
) -> None:
    (synthetic_session / "sync_point.json").unlink()
    manifest = load_session(synthetic_session)
    assert manifest.sync_point == {}
