"""Tests for :class:`LeaderRole` and :class:`FollowerRole`."""

from __future__ import annotations

import pytest

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
        assert FollowerRole().leader_wait_timeout_sec == 60.0

    def test_wait_timeout_override(self):
        assert FollowerRole(leader_wait_timeout_sec=5.0).leader_wait_timeout_sec == 5.0
