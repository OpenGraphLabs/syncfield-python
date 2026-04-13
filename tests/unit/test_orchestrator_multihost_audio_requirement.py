"""Multi-host requires at least one audio-capable stream per host."""

from pathlib import Path

import pytest

import syncfield as sf
from tests.unit.conftest import FakeStream


class TestMultihostAudioRequirement:
    def test_leader_without_audio_stream_raises(self, tmp_path: Path) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042"),
        )
        session.add(FakeStream("cam_main", kind="video"))

        with pytest.raises(ValueError, match="audio-capable stream"):
            session._validate_multihost_audio_requirement()

    def test_follower_without_audio_stream_raises(self, tmp_path: Path) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(session_id="amber-tiger-042"),
        )
        session.add(FakeStream("wrist_cam", kind="video"))

        with pytest.raises(ValueError, match="audio-capable stream"):
            session._validate_multihost_audio_requirement()

    def test_leader_with_audio_stream_passes(self, tmp_path: Path) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_a",
            output_dir=tmp_path,
            role=sf.LeaderRole(session_id="amber-tiger-042"),
        )
        session.add(FakeStream("cam_main", kind="video"))
        session.add(FakeStream("mic", kind="audio"))

        # No raise.
        session._validate_multihost_audio_requirement()

    def test_single_host_without_audio_stream_passes(
        self, tmp_path: Path
    ) -> None:
        # Single-host imposes no audio requirement.
        session = sf.SessionOrchestrator(host_id="mac_a", output_dir=tmp_path)
        session.add(FakeStream("cam_main", kind="video"))

        session._validate_multihost_audio_requirement()

    def test_error_message_includes_host_id_and_role(
        self, tmp_path: Path
    ) -> None:
        session = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(session_id="amber-tiger-042"),
        )
        session.add(FakeStream("wrist_cam", kind="video"))

        with pytest.raises(ValueError) as exc_info:
            session._validate_multihost_audio_requirement()

        msg = str(exc_info.value)
        assert "mac_b" in msg
        assert "follower" in msg.lower()
