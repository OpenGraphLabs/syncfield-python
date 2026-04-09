"""Tests for SessionOrchestrator lifecycle and behavior."""

from __future__ import annotations

import pytest

from syncfield.orchestrator import SessionOrchestrator
from syncfield.testing import FakeStream
from syncfield.tone import SyncToneConfig
from syncfield.types import SessionState


def _session(tmp_path, **kwargs) -> SessionOrchestrator:
    """Construct a silent-chirp session for concise test setup."""
    return SessionOrchestrator(
        host_id=kwargs.pop("host_id", "rig_01"),
        output_dir=tmp_path,
        sync_tone=kwargs.pop("sync_tone", SyncToneConfig.silent()),
        **kwargs,
    )


class TestConstruction:
    def test_initial_state_is_idle(self, tmp_path):
        assert _session(tmp_path).state is SessionState.IDLE

    def test_host_id_property(self, tmp_path):
        assert _session(tmp_path, host_id="rig_42").host_id == "rig_42"

    def test_output_dir_created(self, tmp_path):
        target = tmp_path / "sub" / "dir"
        assert not target.exists()
        SessionOrchestrator(
            host_id="h",
            output_dir=target,
            sync_tone=SyncToneConfig.silent(),
        )
        assert target.exists()


class TestAdd:
    def test_add_stream_in_idle_state(self, tmp_path):
        session = _session(tmp_path)
        session.add(FakeStream("cam"))
        assert session.state is SessionState.IDLE  # add does not change state

    def test_rejects_duplicate_stream_id(self, tmp_path):
        session = _session(tmp_path)
        session.add(FakeStream("cam"))
        with pytest.raises(ValueError, match="duplicate stream id"):
            session.add(FakeStream("cam"))
