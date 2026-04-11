"""Tests for the episode scanning and API helper functions."""

from __future__ import annotations

import json
from pathlib import Path

from syncfield.viewer.server import _scan_episodes


class TestScanEpisodes:
    def test_empty_directory(self, tmp_path: Path):
        episodes = _scan_episodes(tmp_path)
        assert episodes == []

    def test_finds_ep_directories(self, tmp_path: Path):
        (tmp_path / "ep_20260410_194006_abc123").mkdir()
        (tmp_path / "ep_20260409_120000_def456").mkdir()
        (tmp_path / "not_an_episode").mkdir()

        episodes = _scan_episodes(tmp_path)
        ids = [ep["id"] for ep in episodes]

        assert len(episodes) == 2
        assert "ep_20260410_194006_abc123" in ids
        assert "ep_20260409_120000_def456" in ids
        assert "not_an_episode" not in ids

    def test_sorted_newest_first(self, tmp_path: Path):
        (tmp_path / "ep_20260409_120000_aaa").mkdir()
        (tmp_path / "ep_20260410_194006_bbb").mkdir()
        (tmp_path / "ep_20260408_080000_ccc").mkdir()

        episodes = _scan_episodes(tmp_path)
        ids = [ep["id"] for ep in episodes]

        assert ids[0] == "ep_20260410_194006_bbb"
        assert ids[-1] == "ep_20260408_080000_ccc"

    def test_detects_manifest(self, tmp_path: Path):
        ep = tmp_path / "ep_20260410_194006_abc123"
        ep.mkdir()
        manifest = {
            "host_id": "rig_01",
            "streams": {"cam": {"kind": "video"}, "imu": {"kind": "sensor"}},
        }
        (ep / "manifest.json").write_text(json.dumps(manifest))

        episodes = _scan_episodes(tmp_path)
        assert episodes[0]["has_manifest"] is True
        assert episodes[0]["host_id"] == "rig_01"
        assert episodes[0]["stream_count"] == 2

    def test_detects_sync(self, tmp_path: Path):
        ep = tmp_path / "ep_20260410_194006_abc123"
        ep.mkdir()
        (ep / "sync_report.json").write_text("{}")

        episodes = _scan_episodes(tmp_path)
        assert episodes[0]["has_sync"] is True

    def test_no_sync_when_missing(self, tmp_path: Path):
        ep = tmp_path / "ep_20260410_194006_abc123"
        ep.mkdir()

        episodes = _scan_episodes(tmp_path)
        assert episodes[0]["has_sync"] is False

    def test_synced_subdir_detected(self, tmp_path: Path):
        ep = tmp_path / "ep_20260410_194006_abc123"
        ep.mkdir()
        (ep / "synced").mkdir()
        (ep / "synced" / "sync_report.json").write_text("{}")

        episodes = _scan_episodes(tmp_path)
        assert episodes[0]["has_sync"] is True

    def test_parses_timestamp(self, tmp_path: Path):
        ep = tmp_path / "ep_20260410_194006_abc123"
        ep.mkdir()

        episodes = _scan_episodes(tmp_path)
        assert "2026-04-10" in episodes[0]["created_at"]

    def test_no_manifest_defaults(self, tmp_path: Path):
        ep = tmp_path / "ep_20260410_194006_abc123"
        ep.mkdir()

        episodes = _scan_episodes(tmp_path)
        assert episodes[0]["has_manifest"] is False
        assert episodes[0]["host_id"] is None
        assert episodes[0]["stream_count"] == 0
