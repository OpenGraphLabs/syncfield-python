"""Unit tests for the multi-host output file layout gate."""

from pathlib import Path
from unittest.mock import patch

import pytest

import syncfield as sf
from syncfield.multihost.types import SessionAnnouncement
from syncfield.orchestrator import (
    _generate_episode_path,
    _generate_multihost_episode_path,
)


class TestGenerateMultihostEpisodePath:
    def test_inserts_session_id_and_host_id_between_root_and_episode(
        self, tmp_path: Path
    ) -> None:
        result = _generate_multihost_episode_path(
            tmp_path, session_id="amber-tiger-042", host_id="mac_a"
        )

        # Path is <tmp>/amber-tiger-042/mac_a/ep_<timestamp>_<hex>
        parts = result.relative_to(tmp_path).parts
        assert parts[0] == "amber-tiger-042"
        assert parts[1] == "mac_a"
        assert parts[2].startswith("ep_")

    def test_does_not_create_directory_on_disk(self, tmp_path: Path) -> None:
        result = _generate_multihost_episode_path(
            tmp_path, session_id="s", host_id="h"
        )
        assert not result.exists()

    def test_episode_suffix_matches_single_host_format(
        self, tmp_path: Path
    ) -> None:
        # The episode dir name ("ep_<timestamp>_<hex>") must stay
        # identical to the single-host helper so downstream tooling
        # that parses episode filenames keeps working.
        single = _generate_episode_path(tmp_path)
        multi = _generate_multihost_episode_path(
            tmp_path, session_id="s", host_id="h"
        )
        assert single.name.startswith("ep_")
        assert multi.name.startswith("ep_")
        # Both produce a name of the form ep_YYYYMMDD_HHMMSS_<6-hex>
        assert len(single.name.split("_")) == len(multi.name.split("_"))


class TestOrchestratorInitPathSelection:
    def test_single_host_path_unchanged(self, tmp_path: Path) -> None:
        session = sf.SessionOrchestrator(host_id="mac_a", output_dir=tmp_path)
        # Single-host: <output_dir>/ep_<timestamp>_<hex> (no session/host prefix)
        parts = session.output_dir.relative_to(tmp_path).parts
        assert len(parts) == 1
        assert parts[0].startswith("ep_")

    def test_leader_role_uses_multihost_layout(self, tmp_path: Path) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042"),
        )
        parts = session.output_dir.relative_to(tmp_path).parts
        assert len(parts) == 3
        assert parts[0] == "amber-tiger-042"
        assert parts[1] == "mac_a"
        assert parts[2].startswith("ep_")

    def test_follower_with_explicit_id_uses_multihost_layout(
        self, tmp_path: Path
    ) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(session_id="amber-tiger-042"),
        )
        parts = session.output_dir.relative_to(tmp_path).parts
        assert len(parts) == 3
        assert parts[0] == "amber-tiger-042"
        assert parts[1] == "mac_b"
        assert parts[2].startswith("ep_")

    def test_follower_without_session_id_uses_pending_placeholder(
        self, tmp_path: Path
    ) -> None:
        # The session_id isn't known until the follower observes a leader.
        # __init__ must not crash — the path will be regenerated once
        # the leader is observed (covered by the start_new_episode task).
        session = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(),  # no session_id
        )
        # The placeholder session_id dir name ("_pending_session") keeps
        # the layout consistent so subsequent stream registration works.
        parts = session.output_dir.relative_to(tmp_path).parts
        assert len(parts) == 3
        assert parts[0] == "_pending_session"
        assert parts[1] == "mac_b"
        assert parts[2].startswith("ep_")


class TestFollowerPathRewriteAfterLeaderObserved:
    def test_output_dir_is_rewritten_to_real_session_id(
        self, tmp_path: Path
    ) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(),  # auto-discover
        )
        # Simulate the leader having been observed with id "real-id-001".
        observed = SessionAnnouncement(
            session_id="real-id-001",
            host_id="mac_a",
            status="recording",
            sdk_version="0.2.0",
            chirp_enabled=True,
        )
        session._observed_leader = observed

        session._rewrite_output_dir_for_observed_session()

        parts = session.output_dir.relative_to(tmp_path).parts
        assert len(parts) == 3
        assert parts[0] == "real-id-001"
        assert parts[1] == "mac_b"
        assert parts[2].startswith("ep_")

    def test_rewrite_is_a_noop_for_leader(self, tmp_path: Path) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042"),
        )
        before = session.output_dir
        session._rewrite_output_dir_for_observed_session()
        assert session.output_dir == before

    def test_rewrite_is_a_noop_for_single_host(self, tmp_path: Path) -> None:
        session = sf.SessionOrchestrator(host_id="mac_a", output_dir=tmp_path)
        before = session.output_dir
        session._rewrite_output_dir_for_observed_session()
        assert session.output_dir == before


class TestPrepareNextEpisodeMultihost:
    """_prepare_next_episode is the private helper that stop()/cancel()
    call to regenerate the episode path for the next recording. In
    multi-host mode the {session_id}/{host_id} prefix must survive.
    """

    def test_next_episode_keeps_session_and_host_prefix(
        self, tmp_path: Path
    ) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042"),
        )
        # Simulate the state _prepare_next_episode expects after a stop.
        session._episode_dir_created = True

        session._prepare_next_episode()

        parts = session.output_dir.relative_to(tmp_path).parts
        assert len(parts) == 3
        assert parts[0] == "amber-tiger-042"
        assert parts[1] == "mac_a"
        assert parts[2].startswith("ep_")

    def test_next_episode_single_host_unchanged(self, tmp_path: Path) -> None:
        session = sf.SessionOrchestrator(host_id="mac_a", output_dir=tmp_path)
        session._episode_dir_created = True

        session._prepare_next_episode()

        parts = session.output_dir.relative_to(tmp_path).parts
        assert len(parts) == 1
        assert parts[0].startswith("ep_")

    def test_next_episode_auto_discover_follower_uses_observed_leader_id(
        self, tmp_path: Path
    ) -> None:
        """After a follower observes its leader and completes its first
        episode, the second episode must reuse the real (observed)
        session id rather than regress to ``_pending_session``.
        """
        session = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(),  # auto-discover — no preset id
        )
        # Simulate the state after start() ran and observed the leader.
        session._observed_leader = SessionAnnouncement(
            session_id="observed-id-007",
            host_id="mac_a",
            status="recording",
            sdk_version="0.2.0",
            chirp_enabled=True,
        )
        session._episode_dir_created = True

        session._prepare_next_episode()

        parts = session.output_dir.relative_to(tmp_path).parts
        assert len(parts) == 3
        assert parts[0] == "observed-id-007"
        assert parts[1] == "mac_b"
        assert parts[2].startswith("ep_")


class TestMultihostFileTreeEndToEnd:
    def test_leader_writes_under_session_and_host_prefix(
        self, tmp_path: Path
    ) -> None:
        from tests.unit.conftest import FakeStream

        shared_root = tmp_path / "cluster_data"

        leader = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=shared_root,
            role=sf.LeaderRole(session_id="amber-tiger-042"),
        )
        leader.add(FakeStream("cam_main", kind="video"))
        leader.add(FakeStream("mic", kind="audio"))

        # Verify the episode path (without actually running start/stop)
        expected = shared_root / "amber-tiger-042" / "mac_a"
        assert leader.output_dir.parent == expected
        assert leader.output_dir.name.startswith("ep_")

    def test_two_hosts_do_not_share_directory(self, tmp_path: Path) -> None:
        from tests.unit.conftest import FakeStream

        shared_root = tmp_path / "cluster_data"

        leader = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=shared_root,
            role=sf.LeaderRole(session_id="amber-tiger-042"),
        )
        follower = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=shared_root,
            role=sf.FollowerRole(session_id="amber-tiger-042"),
        )
        leader.add(FakeStream("cam_main", kind="video"))
        leader.add(FakeStream("mic_a", kind="audio"))
        follower.add(FakeStream("wrist_cam", kind="video"))
        follower.add(FakeStream("mic_b", kind="audio"))

        assert leader.output_dir.parent.parent == shared_root / "amber-tiger-042"
        assert follower.output_dir.parent.parent == shared_root / "amber-tiger-042"
        # Per-host branches are distinct.
        assert leader.output_dir.parent.name == "mac_a"
        assert follower.output_dir.parent.name == "mac_b"
        # And the episode subdirs are distinct paths even if names collide.
        assert leader.output_dir != follower.output_dir
