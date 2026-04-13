"""Leader-side config distribution: build, discover, distribute."""

from unittest.mock import MagicMock

import httpx
import pytest

import syncfield as sf
from syncfield.multihost.errors import ClusterConfigMismatch
from syncfield.multihost.session_config import SessionConfig
from syncfield.multihost.types import SessionAnnouncement
from tests.unit.conftest import FakeStream


class TestBuildSessionConfig:
    def test_single_host_returns_none(self, tmp_path) -> None:
        session = sf.SessionOrchestrator(host_id="h", output_dir=tmp_path)
        assert session._build_session_config() is None

    def test_leader_builds_config_from_sync_tone(self, tmp_path) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042", control_plane_port=0),
        )
        cfg = session._build_session_config()
        assert isinstance(cfg, SessionConfig)
        assert cfg.session_name == "amber-tiger-042"
        # Chirp specs are copied from self._sync_tone.
        assert cfg.start_chirp == session._sync_tone.start_chirp
        assert cfg.stop_chirp == session._sync_tone.stop_chirp
        assert cfg.recording_mode == "standard"


class TestDiscoverFollowersInPreparing:
    def test_returns_empty_when_no_browser(self, tmp_path) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042", control_plane_port=0),
        )
        assert session._discover_followers_in_preparing() == []

    def test_filters_by_session_id_and_preparing_status(self, tmp_path) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042", control_plane_port=0),
        )
        # Inject a fake browser that has 3 current announcements —
        # one self, one follower with right id, one follower with wrong id.
        fake_browser = MagicMock()
        fake_browser.current_sessions.return_value = [
            SessionAnnouncement(
                session_id="amber-tiger-042", host_id="mac_a",
                status="preparing", sdk_version="0.2.0", chirp_enabled=True,
                control_plane_port=7878,
            ),
            SessionAnnouncement(
                session_id="amber-tiger-042", host_id="mac_b",
                status="preparing", sdk_version="0.2.0", chirp_enabled=True,
                control_plane_port=7979,
            ),
            SessionAnnouncement(
                session_id="different-id-555", host_id="mac_c",
                status="preparing", sdk_version="0.2.0", chirp_enabled=True,
                control_plane_port=7878,
            ),
        ]
        session._browser = fake_browser

        followers = session._discover_followers_in_preparing()
        # Only mac_b qualifies: matching session_id, not self, status=preparing.
        assert len(followers) == 1
        assert followers[0].host_id == "mac_b"
        assert followers[0].control_plane_port == 7979

    def test_skips_followers_with_no_control_plane_port(self, tmp_path) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042", control_plane_port=0),
        )
        fake_browser = MagicMock()
        fake_browser.current_sessions.return_value = [
            SessionAnnouncement(
                session_id="amber-tiger-042", host_id="mac_b",
                status="preparing", sdk_version="0.2.0", chirp_enabled=True,
                control_plane_port=None,  # unreachable
            ),
            SessionAnnouncement(
                session_id="amber-tiger-042", host_id="mac_c",
                status="preparing", sdk_version="0.2.0", chirp_enabled=True,
                control_plane_port=7878,
            ),
        ]
        session._browser = fake_browser

        followers = session._discover_followers_in_preparing()
        assert len(followers) == 1
        assert followers[0].host_id == "mac_c"


class TestDistributeConfigToFollowers:
    def _leader(self, tmp_path, *, with_audio=True):
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042", control_plane_port=0),
        )
        session.add(FakeStream("cam"))
        if with_audio:
            mic = FakeStream("mic"); mic.kind = "audio"
            session.add(mic)
        return session

    def test_noop_when_no_followers(self, tmp_path) -> None:
        session = self._leader(tmp_path)
        session._browser = MagicMock()
        session._browser.current_sessions.return_value = []
        # Should not raise, should not attempt any http call.
        session._distribute_config_to_followers()

    def test_posts_to_each_follower(self, tmp_path, monkeypatch) -> None:
        session = self._leader(tmp_path)
        cfg = session._build_session_config()

        session._browser = MagicMock()
        session._browser.current_sessions.return_value = [
            SessionAnnouncement(
                session_id="amber-tiger-042", host_id="mac_b",
                status="preparing", sdk_version="0.2.0", chirp_enabled=True,
                control_plane_port=7979,
            ),
            SessionAnnouncement(
                session_id="amber-tiger-042", host_id="mac_c",
                status="preparing", sdk_version="0.2.0", chirp_enabled=True,
                control_plane_port=7980,
            ),
        ]
        calls = []

        def fake_post(url, json, headers, timeout):
            calls.append((url, json, headers))
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "session_name": cfg.session_name,
                "start_chirp": cfg.start_chirp.to_dict(),
                "stop_chirp": cfg.stop_chirp.to_dict(),
                "recording_mode": cfg.recording_mode,
            }
            return resp

        monkeypatch.setattr(httpx, "post", fake_post)
        session._distribute_config_to_followers()

        urls = [c[0] for c in calls]
        assert "http://127.0.0.1:7979/session/config" in urls[0] or "mac_b" in urls[0]
        assert "http://127.0.0.1:7980/session/config" in urls[1] or "mac_c" in urls[1]
        # Each call carries bearer token = session id.
        for _, body, headers in calls:
            assert headers["Authorization"] == "Bearer amber-tiger-042"
            assert body["session_name"] == "amber-tiger-042"

    def test_400_from_follower_raises_cluster_config_mismatch(
        self, tmp_path, monkeypatch
    ) -> None:
        session = self._leader(tmp_path)
        session._browser = MagicMock()
        session._browser.current_sessions.return_value = [
            SessionAnnouncement(
                session_id="amber-tiger-042", host_id="mac_b",
                status="preparing", sdk_version="0.2.0", chirp_enabled=True,
                control_plane_port=7979,
            ),
        ]

        def fake_post(url, json, headers, timeout):
            resp = MagicMock()
            resp.status_code = 400
            resp.json.return_value = {"detail": "no audio stream"}
            return resp

        monkeypatch.setattr(httpx, "post", fake_post)
        with pytest.raises(ClusterConfigMismatch) as exc_info:
            session._distribute_config_to_followers()
        assert "mac_b" in exc_info.value.rejections
        assert "no audio stream" in exc_info.value.rejections["mac_b"]

    def test_aggregates_rejections_across_followers(
        self, tmp_path, monkeypatch
    ) -> None:
        session = self._leader(tmp_path)
        session._browser = MagicMock()
        session._browser.current_sessions.return_value = [
            SessionAnnouncement(
                session_id="amber-tiger-042", host_id="mac_b",
                status="preparing", sdk_version="0.2.0", chirp_enabled=True,
                control_plane_port=7979,
            ),
            SessionAnnouncement(
                session_id="amber-tiger-042", host_id="mac_c",
                status="preparing", sdk_version="0.2.0", chirp_enabled=True,
                control_plane_port=7980,
            ),
        ]

        # Both reject for different reasons.
        rejections_by_host = {"mac_b": "reason B", "mac_c": "reason C"}
        def fake_post_distinct(url, json, headers, timeout):
            resp = MagicMock()
            resp.status_code = 400
            host = "mac_b" if ":7979/" in url else "mac_c"
            resp.json.return_value = {"detail": rejections_by_host[host]}
            return resp

        monkeypatch.setattr(httpx, "post", fake_post_distinct)
        with pytest.raises(ClusterConfigMismatch) as exc_info:
            session._distribute_config_to_followers()
        assert set(exc_info.value.rejections.keys()) == {"mac_b", "mac_c"}


class TestRollbackAfterDistributeFailure:
    """The rollback helper invoked by start() when distribute raises.

    Phase 4 Task 7 originally relied on an existing except block to
    catch ClusterConfigMismatch — but that block closes before the
    distribute call runs, so a direct helper is required. These tests
    cover the helper in isolation; full start() integration coverage
    lands in the e2e test (Task 10).
    """

    def test_transitions_from_recording_to_connected(self, tmp_path) -> None:
        from syncfield.types import SessionState

        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042", control_plane_port=0),
        )
        # Simulate post-start state: we're in RECORDING with a fake
        # sync_point anchored and the episode dir marked as created.
        from syncfield.clock import SessionClock, SyncPoint

        session._state = SessionState.RECORDING
        session._episode_dir_created = True
        session._sync_point = SyncPoint.create_now("mac_a")
        session._session_clock = SessionClock(sync_point=session._sync_point)

        session._rollback_after_distribute_failure()

        assert session._state is SessionState.CONNECTED
        assert session._episode_dir_created is False
        assert session._sync_point is None
        assert session._session_clock is None
        assert session._chirp_start is None
        assert session._chirp_stop is None
        assert session._log_writer is None

    def test_stops_recording_streams(self, tmp_path) -> None:
        """Every stream's ``stop_recording`` is invoked during rollback."""
        from syncfield.types import SessionState

        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042", control_plane_port=0),
        )
        cam = FakeStream("cam")
        mic = FakeStream("mic", kind="audio")
        session.add(cam)
        session.add(mic)
        session._state = SessionState.RECORDING

        session._rollback_after_distribute_failure()

        # FakeStream.stop_recording falls through to stop(), which
        # increments stop_calls. A non-zero count proves the helper
        # actually dispatched the tear-down to each stream.
        assert cam.stop_calls == 1
        assert mic.stop_calls == 1
        assert session._state is SessionState.CONNECTED

    def test_swallows_stream_stop_errors(self, tmp_path) -> None:
        """A raising stream must not prevent the rest of the rollback."""
        from syncfield.types import SessionState

        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042", control_plane_port=0),
        )
        bad = FakeStream("bad")

        def raising_stop() -> None:
            raise RuntimeError("boom")

        bad.stop_recording = raising_stop  # type: ignore[assignment]
        session.add(bad)
        session._state = SessionState.RECORDING

        # Must not raise — rollback is best-effort on the stream side.
        session._rollback_after_distribute_failure()
        assert session._state is SessionState.CONNECTED


class TestFollowerFetchConfig:
    def test_noop_without_observed_leader(self, tmp_path) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(control_plane_port=0),
        )
        # No _observed_leader set.
        session._fetch_config_from_leader()  # should not raise

    def test_noop_when_leader_has_no_control_plane_port(self, tmp_path) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(control_plane_port=0),
        )
        session._observed_leader = SessionAnnouncement(
            session_id="amber-tiger-042", host_id="mac_a",
            status="recording", sdk_version="0.2.0", chirp_enabled=True,
            control_plane_port=None,
        )
        session._fetch_config_from_leader()  # should not raise

    def test_fetches_and_applies_config(self, tmp_path, monkeypatch) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(control_plane_port=0),
        )
        session.add(FakeStream("wrist"))
        mic = FakeStream("mic"); mic.kind = "audio"
        session.add(mic)

        session._observed_leader = SessionAnnouncement(
            session_id="amber-tiger-042", host_id="mac_a",
            status="recording", sdk_version="0.2.0", chirp_enabled=True,
            control_plane_port=7878,
        )

        def fake_get(url, headers, timeout):
            assert "127.0.0.1:7878" in url
            assert headers["Authorization"] == "Bearer amber-tiger-042"
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "session_name": "amber-tiger-042",
                "start_chirp": {"from_hz": 400.0, "to_hz": 2500.0, "duration_ms": 500,
                                "amplitude": 0.8, "envelope_ms": 15},
                "stop_chirp": {"from_hz": 2500.0, "to_hz": 400.0, "duration_ms": 500,
                               "amplitude": 0.8, "envelope_ms": 15},
                "recording_mode": "standard",
            }
            return resp

        monkeypatch.setattr(httpx, "get", fake_get)
        session._fetch_config_from_leader()

        assert session._applied_session_config is not None
        assert session._applied_session_config.session_name == "amber-tiger-042"


class TestLeaderBootstrapsBrowser:
    def test_distribute_starts_and_stops_short_lived_browser(
        self, tmp_path, monkeypatch
    ) -> None:
        """Without a test-injected browser, distribute must bootstrap its own."""
        from syncfield.multihost.browser import SessionBrowser

        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042", control_plane_port=0),
        )

        started = []
        closed = []

        class _FakeBrowser:
            def __init__(self, *, session_id=None):
                started.append(session_id)
                self._closed = False
            def start(self):
                pass
            def current_sessions(self):
                return []  # no followers — exercises the 'no followers' branch
            def close(self):
                closed.append(True)

        monkeypatch.setattr(
            "syncfield.multihost.browser.SessionBrowser", _FakeBrowser
        )
        # Patch time.sleep so the test doesn't wait 1.5s — the method
        # does `import time as _time` locally then calls `_time.sleep(1.5)`,
        # and since `_time` is just a local alias for the `time` module,
        # patching `time.sleep` on the real module is sufficient.
        monkeypatch.setattr("time.sleep", lambda *_: None)

        session._distribute_config_to_followers()

        assert started == ["amber-tiger-042"], (
            "Leader must bootstrap a SessionBrowser filtered by session_id"
        )
        assert closed == [True], "Leader must close its browser after distribute"
        # Zero-follower path still sets applied_session_config.
        assert session._applied_session_config is not None
        assert session._applied_session_config.session_name == "amber-tiger-042"

    def test_does_not_double_bootstrap_when_browser_exists(
        self, tmp_path, monkeypatch
    ) -> None:
        """If a browser is already attached (test or follower path), don't replace it."""
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042", control_plane_port=0),
        )
        existing = MagicMock()
        existing.current_sessions.return_value = []
        session._browser = existing

        session._distribute_config_to_followers()

        # Existing browser was used, not closed (caller owns it).
        existing.close.assert_not_called()
        assert session._browser is existing


class TestFollowerPostPropagatesToOrchestrator:
    def test_post_populates_applied_session_config(self, tmp_path) -> None:
        """A successful POST must propagate into orchestrator._applied_session_config."""
        follower = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(session_id="amber-tiger-042", control_plane_port=0),
        )
        follower.add(FakeStream("cam"))
        mic = FakeStream("mic"); mic.kind = "audio"
        follower.add(mic)
        follower._start_control_plane_only_for_tests()

        try:
            import httpx
            payload = {
                "session_name": "amber-tiger-042",
                "start_chirp": {"from_hz": 400.0, "to_hz": 2500.0, "duration_ms": 500,
                                "amplitude": 0.8, "envelope_ms": 15},
                "stop_chirp": {"from_hz": 2500.0, "to_hz": 400.0, "duration_ms": 500,
                               "amplitude": 0.8, "envelope_ms": 15},
                "recording_mode": "standard",
            }
            r = httpx.post(
                f"http://127.0.0.1:{follower._control_plane.actual_port}/session/config",
                json=payload,
                headers={"Authorization": "Bearer amber-tiger-042"},
                timeout=2.0,
            )
            assert r.status_code == 200

            # C2 fix: orchestrator attribute must match, not just app.state.
            assert follower._applied_session_config is not None
            assert follower._applied_session_config.session_name == "amber-tiger-042"
        finally:
            follower._stop_control_plane_only_for_tests()
