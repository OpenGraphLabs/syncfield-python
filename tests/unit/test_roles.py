"""Tests for :class:`LeaderRole` and :class:`FollowerRole`."""

from __future__ import annotations

import pytest

import syncfield as sf
from syncfield.roles import FollowerRole, LeaderRole


class TestLeaderRole:
    def test_generates_session_id_when_missing(self):
        role = LeaderRole()
        assert role.session_id is not None
        assert len(role.session_id) > 0

    def test_respects_explicit_session_id(self):
        role = LeaderRole(session_id="amber-tiger-042")
        assert role.session_id == "amber-tiger-042"

    def test_rejects_invalid_session_id(self):
        with pytest.raises(ValueError, match="session_id"):
            LeaderRole(session_id="has space")

    def test_rejects_dot_in_session_id(self):
        with pytest.raises(ValueError, match="session_id"):
            LeaderRole(session_id="foo.bar")

    def test_kind_is_leader(self):
        assert LeaderRole().kind == "leader"

    def test_default_graceful_shutdown_ms(self):
        assert LeaderRole().graceful_shutdown_ms == 1000

    def test_graceful_shutdown_ms_override(self):
        assert LeaderRole(graceful_shutdown_ms=0).graceful_shutdown_ms == 0


class TestFollowerRole:
    def test_default_allows_auto_discovery(self):
        role = FollowerRole()
        assert role.session_id is None

    def test_explicit_session_id_ok(self):
        role = FollowerRole(session_id="amber-tiger-042")
        assert role.session_id == "amber-tiger-042"

    def test_rejects_invalid_session_id(self):
        with pytest.raises(ValueError, match="session_id"):
            FollowerRole(session_id="has space")

    def test_kind_is_follower(self):
        assert FollowerRole().kind == "follower"

    def test_default_wait_timeout(self):
        assert FollowerRole().leader_wait_timeout_sec == 3600.0

    def test_wait_timeout_override(self):
        assert FollowerRole(leader_wait_timeout_sec=5.0).leader_wait_timeout_sec == 5.0


class TestControlPlaneConfig:
    def test_leader_role_defaults(self) -> None:
        r = sf.LeaderRole(session_id="amber-tiger-042")
        assert r.control_plane_port == 7878
        assert r.keep_alive_after_stop_sec == 600.0

    def test_leader_role_accepts_overrides(self) -> None:
        r = sf.LeaderRole(
            session_id="amber-tiger-042",
            control_plane_port=9090,
            keep_alive_after_stop_sec=120.0,
        )
        assert r.control_plane_port == 9090
        assert r.keep_alive_after_stop_sec == 120.0

    def test_follower_role_defaults(self) -> None:
        r = sf.FollowerRole()
        assert r.control_plane_port == 7878
        assert r.keep_alive_after_stop_sec == 600.0

    def test_follower_role_accepts_overrides(self) -> None:
        r = sf.FollowerRole(
            control_plane_port=0,  # pure OS-assigned
            keep_alive_after_stop_sec=0.5,
        )
        assert r.control_plane_port == 0
        assert r.keep_alive_after_stop_sec == 0.5


class TestControlPlaneDefaultsStayInSync:
    def test_role_defaults_match_control_plane_module(self) -> None:
        from syncfield.multihost.control_plane import (
            DEFAULT_CONTROL_PLANE_PORT,
            DEFAULT_KEEP_ALIVE_AFTER_STOP_SEC,
        )

        assert sf.LeaderRole().control_plane_port == DEFAULT_CONTROL_PLANE_PORT
        assert sf.LeaderRole().keep_alive_after_stop_sec == DEFAULT_KEEP_ALIVE_AFTER_STOP_SEC
        assert sf.FollowerRole().control_plane_port == DEFAULT_CONTROL_PLANE_PORT
        assert sf.FollowerRole().keep_alive_after_stop_sec == DEFAULT_KEEP_ALIVE_AFTER_STOP_SEC
