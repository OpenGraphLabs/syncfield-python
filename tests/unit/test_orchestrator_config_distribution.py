"""Leader-side config distribution: build, discover, distribute."""
from __future__ import annotations

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


class TestFollowerBaseURL:
    """The shared URL builder used by both distribute and fetch paths.

    Phase 5 plumbs the follower's real LAN address via the
    ``resolved_address`` field on ``SessionAnnouncement`` (populated
    by the browser from the mDNS ``ServiceInfo``). The helper falls
    back to loopback when the field is unset so localhost tests keep
    working.
    """

    def test_uses_resolved_address_when_set(self) -> None:
        ann = SessionAnnouncement(
            session_id="amber-tiger-042", host_id="mac_b",
            status="preparing", sdk_version="0.2.0", chirp_enabled=True,
            control_plane_port=7878, resolved_address="192.168.1.5",
        )
        assert (
            sf.SessionOrchestrator._follower_base_url(ann)
            == "http://192.168.1.5:7878"
        )

    def test_falls_back_to_loopback(self) -> None:
        ann = SessionAnnouncement(
            session_id="amber-tiger-042", host_id="mac_b",
            status="preparing", sdk_version="0.2.0", chirp_enabled=True,
            control_plane_port=7979, resolved_address=None,
        )
        url = sf.SessionOrchestrator._follower_base_url(ann)
        assert url.startswith("http://127.0.0.1:")
        assert url == "http://127.0.0.1:7979"


class TestDistributeUsesResolvedAddress:
    """Distribute must build URLs from resolved_address when present."""

    def test_post_url_uses_resolved_address(self, tmp_path, monkeypatch) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042", control_plane_port=0),
        )
        session.add(FakeStream("cam"))
        mic = FakeStream("mic"); mic.kind = "audio"
        session.add(mic)
        cfg = session._build_session_config()

        session._browser = MagicMock()
        session._browser.current_sessions.return_value = [
            SessionAnnouncement(
                session_id="amber-tiger-042", host_id="mac_b",
                status="preparing", sdk_version="0.2.0", chirp_enabled=True,
                control_plane_port=7979, resolved_address="192.168.1.42",
            ),
        ]
        captured_urls: list[str] = []

        def fake_post(url, json, headers, timeout):
            captured_urls.append(url)
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

        assert captured_urls == ["http://192.168.1.42:7979/session/config"]


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
        # Since refactor 64dd0fd the orchestrator brings up a browser at
        # __init__ time. The conftest replaces it with _InertBrowser, so
        # the "_browser is None" bootstrap branch we want to exercise
        # here is skipped. Drop the inert browser to force the fallback.
        session._browser = None

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


class TestFollowerAdvertising:
    def test_preshared_follower_advertises_at_start(self, tmp_path, monkeypatch):
        """Regression: followers with pre-shared session_id must advertise
        themselves on mDNS so the leader can discover them."""
        from unittest.mock import MagicMock
        import syncfield as sf
        from syncfield.multihost import advertiser as adv_module
        from tests.unit.conftest import FakeStream

        created_advertisers = []

        class _FakeAdvertiser:
            def __init__(self, **kwargs):
                created_advertisers.append(kwargs)
                self.kwargs = kwargs
            def start(self): pass
            def update_status(self, *a, **kw): pass
            def close(self): pass

        monkeypatch.setattr(adv_module, "SessionAdvertiser", _FakeAdvertiser)
        # Also patch the symbol in orchestrator.py's namespace if the
        # orchestrator does `from syncfield.multihost.advertiser import SessionAdvertiser`.
        import syncfield.orchestrator as orch_mod
        monkeypatch.setattr(orch_mod, "SessionAdvertiser", _FakeAdvertiser, raising=False)

        follower = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(session_id="amber-tiger-042", control_plane_port=0),
        )
        # Pre-shared followers create an init-time advertiser as part of
        # mDNS bring-up (refactor 64dd0fd). Clear the counter so the rest
        # of the test only measures advertisers created by the explicit
        # _maybe_start_advertising() call below.
        created_advertisers.clear()
        follower.add(FakeStream("cam"))
        mic = FakeStream("mic"); mic.kind = "audio"
        follower.add(mic)

        # Trigger the advertiser-start code path without running a real
        # session.start() (which would block waiting for a leader).
        follower._start_control_plane_only_for_tests()
        try:
            follower._maybe_start_advertising()

            assert len(created_advertisers) == 1
            kwargs = created_advertisers[0]
            assert kwargs["session_id"] == "amber-tiger-042"
            assert kwargs["host_id"] == "mac_b"
            assert kwargs["control_plane_port"] == follower._control_plane.actual_port
        finally:
            follower._stop_control_plane_only_for_tests()

    def test_auto_discover_follower_does_not_advertise_pre_observation(
        self, tmp_path, monkeypatch
    ):
        """Auto-discover follower must NOT start its advertiser until
        _maybe_wait_for_leader has observed the leader."""
        import syncfield as sf
        import syncfield.orchestrator as orch_mod
        from tests.unit.conftest import FakeStream

        class _FakeAdvertiser:
            created = 0
            def __init__(self, **kwargs):
                _FakeAdvertiser.created += 1
            def start(self): pass
            def close(self): pass

        monkeypatch.setattr(orch_mod, "SessionAdvertiser", _FakeAdvertiser, raising=False)

        follower = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(control_plane_port=0),  # no session_id
        )
        follower.add(FakeStream("cam"))
        mic = FakeStream("mic"); mic.kind = "audio"
        follower.add(mic)

        follower._start_control_plane_only_for_tests()
        try:
            follower._maybe_start_advertising()  # session_id still None
            assert _FakeAdvertiser.created == 0
        finally:
            follower._stop_control_plane_only_for_tests()


class TestStaticPeers:
    def test_set_static_peers_short_circuits_mdns_in_discovery(self, tmp_path):
        """Static peers bypass the mDNS browser entirely."""
        import syncfield as sf
        from tests.unit.conftest import FakeStream

        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="sid", control_plane_port=0),
        )
        session.add(FakeStream("cam"))
        mic = FakeStream("mic"); mic.kind = "audio"
        session.add(mic)

        # Inject static peers.
        session.set_static_peers([
            {"host_id": "mac_b", "control_plane_port": 7879,
             "resolved_address": "127.0.0.1", "status": "preparing"},
        ])

        # Drop the conftest-installed inert browser so the assertion below
        # genuinely proves the discovery path doesn't touch any browser.
        # (Since refactor 64dd0fd a browser is wired up at __init__.)
        session._browser = None
        assert session._browser is None

        peers = session._discover_followers_in_preparing()
        assert len(peers) == 1
        assert peers[0].host_id == "mac_b"
        assert peers[0].control_plane_port == 7879
        assert peers[0].resolved_address == "127.0.0.1"

    def test_static_peers_excludes_self(self, tmp_path):
        """Self should be filtered out even from static peers."""
        import syncfield as sf

        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="sid", control_plane_port=0),
        )
        session.set_static_peers([
            {"host_id": "mac_a", "control_plane_port": 7878,
             "resolved_address": "127.0.0.1", "status": "preparing"},
            {"host_id": "mac_b", "control_plane_port": 7879,
             "resolved_address": "127.0.0.1", "status": "preparing"},
        ])
        peers = session._discover_followers_in_preparing()
        assert [p.host_id for p in peers] == ["mac_b"]


class TestStaticLeader:
    def test_set_static_leader_populates_attribute(self, tmp_path):
        import syncfield as sf

        session = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(session_id="sid", control_plane_port=0),
        )
        session.set_static_leader("mac_a", "127.0.0.1", 7878)

        assert session._static_leader is not None
        assert session._static_leader.host_id == "mac_a"
        assert session._static_leader.resolved_address == "127.0.0.1"
        assert session._static_leader.control_plane_port == 7878


class TestAutoDiscoverFollowerAdvertisesOnPreparing:
    def test_follower_advertises_after_observing_preparing_leader(
        self, tmp_path, monkeypatch
    ):
        """Regression: auto-discover follower must start advertising as
        soon as it sees a leader in PREPARING, not wait for RECORDING.
        Otherwise leader's distribute runs against an empty peer list.
        """
        import syncfield as sf
        from tests.unit.conftest import FakeStream
        from syncfield.multihost.types import SessionAnnouncement
        import syncfield.orchestrator as orch_mod

        created_advertisers: list = []

        class _FakeAdvertiser:
            def __init__(self, **kwargs):
                created_advertisers.append(kwargs)
            def start(self): pass
            def update_status(self, *a, **kw): pass
            def close(self): pass

        class _FakeBrowser:
            def __init__(self, session_id=None):
                self.session_id_filter = session_id
                self._observed: SessionAnnouncement | None = None
            def start(self): pass
            def close(self): pass
            def wait_for_observation(self, timeout=30.0):
                # Simulate: leader in preparing state observed immediately.
                return SessionAnnouncement(
                    session_id="auto-generated-session",
                    host_id="mac_a",
                    status="preparing",
                    sdk_version="0.2.0",
                    chirp_enabled=True,
                    control_plane_port=7878,
                )
            def wait_for_recording(self, timeout=30.0):
                return SessionAnnouncement(
                    session_id="auto-generated-session",
                    host_id="mac_a",
                    status="recording",
                    sdk_version="0.2.0",
                    chirp_enabled=True,
                    control_plane_port=7878,
                )
            def current_sessions(self): return []

        monkeypatch.setattr(orch_mod, "SessionAdvertiser", _FakeAdvertiser, raising=False)
        monkeypatch.setattr(orch_mod, "SessionBrowser", _FakeBrowser, raising=False)

        follower = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(control_plane_port=0),  # auto-discover
        )
        follower.add(FakeStream("cam"))
        mic = FakeStream("mic"); mic.kind = "audio"
        follower.add(mic)

        # Drive _maybe_wait_for_leader directly.
        follower._start_control_plane_only_for_tests()
        try:
            follower._maybe_wait_for_leader()

            # Advertiser MUST have started after the first observation,
            # even though the leader was only in 'preparing' at that
            # point (not recording).
            assert len(created_advertisers) == 1
            assert created_advertisers[0]["session_id"] == "auto-generated-session"
            assert created_advertisers[0]["host_id"] == "mac_b"
        finally:
            follower._stop_control_plane_only_for_tests()
