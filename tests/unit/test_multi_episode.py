"""Tests for multi-episode recording resilience.

Exercises the record→stop→record→stop cycle, cancel flows, and
edge cases to ensure the orchestrator produces exactly one episode
directory per completed recording with all artifacts in the right place.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncfield.orchestrator import SessionOrchestrator
from syncfield.testing import FakeStream
from syncfield.tone import SyncToneConfig


def _session(tmp_path: Path) -> SessionOrchestrator:
    return SessionOrchestrator(
        host_id="rig_01",
        output_dir=tmp_path,
        sync_tone=SyncToneConfig.silent(),
    )


class TestMultiEpisodeRecording:
    """Record → Stop → Record → Stop must produce two separate episodes."""

    def test_two_consecutive_episodes(self, tmp_path: Path):
        session = _session(tmp_path)
        session.add(FakeStream("cam"))

        # Episode 1
        session.start()
        ep1_dir = session.output_dir
        assert ep1_dir.exists()
        report1 = session.stop()
        assert report1.finalizations[0].status == "completed"

        # Episode 2 — must get a NEW directory
        session.start()
        ep2_dir = session.output_dir
        assert ep2_dir.exists()
        assert ep2_dir != ep1_dir, "Second recording must use a new episode dir"
        report2 = session.stop()
        assert report2.finalizations[0].status == "completed"

        # Both episodes have their own manifest
        assert (ep1_dir / "manifest.json").exists()
        assert (ep2_dir / "manifest.json").exists()

    def test_five_consecutive_episodes(self, tmp_path: Path):
        session = _session(tmp_path)
        session.add(FakeStream("cam"))

        dirs = []
        for i in range(5):
            session.start()
            dirs.append(session.output_dir)
            session.stop()

        # All 5 dirs are unique
        assert len(set(dirs)) == 5

        # All 5 have manifest
        for d in dirs:
            assert (d / "manifest.json").exists()

    def test_no_dir_before_start(self, tmp_path: Path):
        session = _session(tmp_path)
        session.add(FakeStream("cam"))

        # Before start, episode dir should NOT exist
        assert not session.output_dir.exists()

    def test_connect_disconnect_no_dir(self, tmp_path: Path):
        session = _session(tmp_path)
        session.add(FakeStream("cam"))

        session.connect()
        session.disconnect()

        # No ep_ directories should exist in the data root
        ep_dirs = list(tmp_path.glob("ep_*"))
        assert len(ep_dirs) == 0


class TestCancelFlow:
    """Cancel must discard data and prepare for the next recording."""

    def test_cancel_deletes_episode(self, tmp_path: Path):
        session = _session(tmp_path)
        session.add(FakeStream("cam"))

        session.start()
        cancelled_dir = session.output_dir
        assert cancelled_dir.exists()

        session.cancel()
        assert not cancelled_dir.exists(), "Cancel must delete episode dir"

    def test_cancel_then_record(self, tmp_path: Path):
        session = _session(tmp_path)
        session.add(FakeStream("cam"))

        # Cancel first attempt
        session.start()
        cancelled_dir = session.output_dir
        session.cancel()

        # Record second attempt
        session.start()
        new_dir = session.output_dir
        assert new_dir != cancelled_dir
        assert new_dir.exists()

        report = session.stop()
        assert report.finalizations[0].status == "completed"
        assert (new_dir / "manifest.json").exists()
        assert not cancelled_dir.exists()

    def test_multiple_cancels_then_record(self, tmp_path: Path):
        session = _session(tmp_path)
        session.add(FakeStream("cam"))

        cancelled_dirs = []
        for _ in range(3):
            session.start()
            cancelled_dirs.append(session.output_dir)
            session.cancel()

        # All cancelled dirs should be gone
        for d in cancelled_dirs:
            assert not d.exists()

        # Final recording should work
        session.start()
        final_dir = session.output_dir
        session.stop()
        assert (final_dir / "manifest.json").exists()

    def test_record_stop_cancel_record_stop(self, tmp_path: Path):
        """Mixed flow: record→stop, record→cancel, record→stop."""
        session = _session(tmp_path)
        session.add(FakeStream("cam"))

        # Episode 1: completed
        session.start()
        ep1 = session.output_dir
        session.stop()

        # Episode 2: cancelled
        session.start()
        ep2_cancelled = session.output_dir
        session.cancel()

        # Episode 3: completed
        session.start()
        ep3 = session.output_dir
        session.stop()

        assert (ep1 / "manifest.json").exists()
        assert not ep2_cancelled.exists()
        assert (ep3 / "manifest.json").exists()
        assert len({ep1, ep2_cancelled, ep3}) == 3


class TestEpisodeIsolation:
    """Each episode's data must be fully contained in its own directory."""

    def test_stream_files_in_correct_episode(self, tmp_path: Path):
        session = _session(tmp_path)
        cam = FakeStream("cam")
        session.add(cam)

        session.start()
        ep1 = session.output_dir
        cam.push_sample(0, 100)
        session.stop()

        session.start()
        ep2 = session.output_dir
        cam.push_sample(0, 200)
        session.stop()

        # Each episode has its own timestamps file
        assert (ep1 / "cam.timestamps.jsonl").exists()
        assert (ep2 / "cam.timestamps.jsonl").exists()

        # Manifests point to correct episodes
        m1 = json.loads((ep1 / "manifest.json").read_text())
        m2 = json.loads((ep2 / "manifest.json").read_text())
        assert m1["host_id"] == "rig_01"
        assert m2["host_id"] == "rig_01"

    def test_no_stale_dirs_after_full_lifecycle(self, tmp_path: Path):
        """Only completed episodes should leave directories."""
        session = _session(tmp_path)
        session.add(FakeStream("cam"))

        session.connect()
        session.disconnect()  # No recording
        session.connect()
        session.start()
        session.cancel()  # Cancelled
        session.start()
        session.stop()  # Completed

        ep_dirs = sorted(tmp_path.glob("ep_*"))
        assert len(ep_dirs) == 1, f"Expected 1 episode dir, got {len(ep_dirs)}: {ep_dirs}"
        assert (ep_dirs[0] / "manifest.json").exists()
