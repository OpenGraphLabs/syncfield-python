"""End-to-end Phase 4: leader control plane <-> follower control plane."""

import time

import httpx

import syncfield as sf
from syncfield.multihost.errors import ClusterConfigMismatch
from syncfield.multihost.session_config import SessionConfig
from syncfield.multihost.types import SessionAnnouncement
from tests.unit.conftest import FakeStream


def _with_audio(session) -> None:
    stream = FakeStream("mic"); stream.kind = "audio"
    session.add(stream)


class TestPhase4E2E:
    def test_leader_posts_follower_applies_get_reflects(self, tmp_path) -> None:
        leader = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path / "leader",
            role=sf.LeaderRole(
                session_id="amber-tiger-042",
                control_plane_port=0,
                keep_alive_after_stop_sec=1.0,
            ),
        )
        leader.add(FakeStream("cam"))
        _with_audio(leader)
        leader._start_control_plane_only_for_tests()

        follower = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path / "follower",
            role=sf.FollowerRole(
                session_id="amber-tiger-042",
                control_plane_port=0,
                keep_alive_after_stop_sec=1.0,
            ),
        )
        follower.add(FakeStream("wrist"))
        _with_audio(follower)
        follower._start_control_plane_only_for_tests()

        try:
            # Simulate: leader sees follower advertising on 'preparing'.
            from unittest.mock import MagicMock
            fake_browser = MagicMock()
            fake_browser.current_sessions.return_value = [
                SessionAnnouncement(
                    session_id="amber-tiger-042",
                    host_id="mac_b",
                    status="preparing",
                    sdk_version="0.2.0",
                    chirp_enabled=True,
                    control_plane_port=follower._control_plane.actual_port,
                ),
            ]
            leader._browser = fake_browser

            # Leader distributes.
            leader._distribute_config_to_followers()

            # Follower has applied the config (GET its own /session/config).
            r = httpx.get(
                f"http://127.0.0.1:{follower._control_plane.actual_port}/session/config",
                headers={"Authorization": "Bearer amber-tiger-042"},
                timeout=1.0,
            )
            assert r.status_code == 200
            body = r.json()
            assert body["session_name"] == "amber-tiger-042"
            assert body["start_chirp"]["from_hz"] == leader._sync_tone.start_chirp.from_hz
        finally:
            leader._stop_control_plane_only_for_tests()
            follower._stop_control_plane_only_for_tests()

    def test_follower_without_audio_forces_cluster_mismatch(self, tmp_path) -> None:
        leader = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path / "leader",
            role=sf.LeaderRole(
                session_id="amber-tiger-042",
                control_plane_port=0,
                keep_alive_after_stop_sec=1.0,
            ),
        )
        leader.add(FakeStream("cam"))
        _with_audio(leader)
        leader._start_control_plane_only_for_tests()

        # Follower WITHOUT audio stream — will reject POST /session/config.
        follower = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path / "follower",
            role=sf.FollowerRole(
                session_id="amber-tiger-042",
                control_plane_port=0,
                keep_alive_after_stop_sec=1.0,
            ),
        )
        follower.add(FakeStream("wrist"))
        _with_audio(follower)  # required by Phase 1 audio gate on start
        # But we'll swap the follower's adapter to report has_audio_stream=False
        # to simulate a mid-flight capability mismatch.
        follower._control_plane = None
        follower._start_control_plane_only_for_tests()

        # Monkey-patch the follower's adapter to report no-audio.
        follower._control_plane._server.config.app.state.orchestrator  # touch
        # Replace the 'orchestrator' on app.state with a proxy that returns False.
        app = follower._control_plane._server.config.app
        real = app.state.orchestrator
        class _NoAudioProxy:
            def __init__(self, inner):
                self._inner = inner
            def __getattr__(self, name):
                if name == "has_audio_stream":
                    return False
                return getattr(self._inner, name)
        app.state.orchestrator = _NoAudioProxy(real)

        try:
            from unittest.mock import MagicMock
            fake_browser = MagicMock()
            fake_browser.current_sessions.return_value = [
                SessionAnnouncement(
                    session_id="amber-tiger-042",
                    host_id="mac_b",
                    status="preparing",
                    sdk_version="0.2.0",
                    chirp_enabled=True,
                    control_plane_port=follower._control_plane.actual_port,
                ),
            ]
            leader._browser = fake_browser

            import pytest
            with pytest.raises(ClusterConfigMismatch) as exc_info:
                leader._distribute_config_to_followers()
            assert "mac_b" in exc_info.value.rejections
            assert "audio" in exc_info.value.rejections["mac_b"]
        finally:
            leader._stop_control_plane_only_for_tests()
            follower._stop_control_plane_only_for_tests()
