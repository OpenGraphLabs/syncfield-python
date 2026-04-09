"""Fixtures for replay loader and server tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


@pytest.fixture
def synthetic_session(tmp_path: Path) -> Path:
    """Build a minimal session folder on disk and return its path."""
    session = tmp_path / "session_test"
    session.mkdir()

    _write_json(
        session / "manifest.json",
        {
            "sdk_version": "0.2.0",
            "host_id": "test_rig",
            "streams": {
                "cam_ego": {
                    "kind": "video",
                    "capabilities": {
                        "provides_audio_track": True,
                        "supports_precise_timestamps": True,
                        "is_removable": False,
                        "produces_file": True,
                    },
                    "status": "completed",
                    "frame_count": 60,
                },
                "wrist_imu": {
                    "kind": "sensor",
                    "capabilities": {
                        "provides_audio_track": False,
                        "supports_precise_timestamps": True,
                        "is_removable": False,
                        "produces_file": False,
                    },
                    "status": "completed",
                    "frame_count": 600,
                },
            },
        },
    )

    _write_json(
        session / "sync_point.json",
        {
            "sdk_version": "0.2.0",
            "monotonic_ns": 100_000_000_000,
            "wall_clock_ns": 1_775_000_000_000_000_000,
            "host_id": "test_rig",
            "timestamp_ms": 1_775_000_000_000,
            "iso_datetime": "2026-04-09T00:00:00",
        },
    )

    # Fake "video" file — content is irrelevant to the loader, only the
    # path matters. Use a few bytes so Range tests have something to slice.
    (session / "cam_ego.mp4").write_bytes(b"\x00MP4FAKE\x00" * 64)

    # Sensor jsonl with two samples
    (session / "wrist_imu.jsonl").write_text(
        '{"t_ns":0,"channels":{"ax":0.1}}\n'
        '{"t_ns":1000000,"channels":{"ax":0.2}}\n'
    )

    return session


@pytest.fixture
def synced_session(synthetic_session: Path) -> Path:
    """A session that also has a synced/sync_report.json."""
    synced = synthetic_session / "synced"
    synced.mkdir()
    _write_json(
        synced / "sync_report.json",
        {
            "streams": {
                "cam_ego": {
                    "offset_seconds": 0.012,
                    "confidence": 0.97,
                    "quality": "excellent",
                },
                "wrist_imu": {
                    "offset_seconds": -0.034,
                    "confidence": 0.81,
                    "quality": "good",
                },
            },
        },
    )
    return synthetic_session
