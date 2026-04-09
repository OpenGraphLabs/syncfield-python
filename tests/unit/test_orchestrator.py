"""Tests for SessionOrchestrator lifecycle and behavior."""

from __future__ import annotations

import pytest

from syncfield.clock import SessionClock
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


class TestStartHappyPath:
    def test_start_transitions_to_recording(self, tmp_path):
        session = _session(tmp_path)
        session.add(FakeStream("cam"))
        session.start()
        assert session.state is SessionState.RECORDING

    def test_start_calls_prepare_then_start_on_each_stream(self, tmp_path):
        session = _session(tmp_path)
        fs1 = FakeStream("a")
        fs2 = FakeStream("b")
        session.add(fs1)
        session.add(fs2)
        session.start()
        assert fs1.prepare_calls == 1
        assert fs1.start_calls == 1
        assert fs2.prepare_calls == 1
        assert fs2.start_calls == 1

    def test_start_cannot_be_called_twice(self, tmp_path):
        session = _session(tmp_path)
        session.add(FakeStream("x"))
        session.start()
        with pytest.raises(RuntimeError, match="start.*recording"):
            session.start()

    def test_start_requires_at_least_one_stream(self, tmp_path):
        session = _session(tmp_path)
        with pytest.raises(RuntimeError, match="no streams"):
            session.start()

    def test_session_clock_shared_across_streams(self, tmp_path):
        """All streams must see the exact same sync point instance."""
        session = _session(tmp_path)

        clocks: list[SessionClock] = []

        class RecordingStream(FakeStream):
            def start(self, session_clock):  # type: ignore[override]
                clocks.append(session_clock)
                super().start(session_clock)

        session.add(RecordingStream("a"))
        session.add(RecordingStream("b"))
        session.start()
        assert len(clocks) == 2
        assert clocks[0].sync_point is clocks[1].sync_point
        assert clocks[0].host_id == "rig_01"


class TestStartRollback:
    def test_failure_during_start_rolls_back_prior_streams(self, tmp_path):
        session = _session(tmp_path)
        good1 = FakeStream("a")
        bad = FakeStream("b", fail_on_start=True)
        good2 = FakeStream("c")
        session.add(good1)
        session.add(bad)
        session.add(good2)

        with pytest.raises(RuntimeError, match="fake failure in start"):
            session.start()

        # good1 was started → must be rolled back (stop called)
        assert good1.start_calls == 1
        assert good1.stop_calls == 1
        # bad raised during start → stop should NOT be called on it
        assert bad.start_calls == 1
        assert bad.stop_calls == 0
        # good2 never reached start
        assert good2.start_calls == 0
        assert good2.stop_calls == 0

        assert session.state is SessionState.IDLE

    def test_failure_during_prepare_stops_earlier_streams(self, tmp_path):
        session = _session(tmp_path)
        good = FakeStream("a")
        bad = FakeStream("b", fail_on_prepare=True)
        session.add(good)
        session.add(bad)

        with pytest.raises(RuntimeError, match="fake failure in prepare"):
            session.start()

        assert good.prepare_calls == 1
        assert good.start_calls == 1  # fully started
        assert good.stop_calls == 1  # then rolled back
        assert bad.prepare_calls == 1
        assert bad.start_calls == 0  # never reached start

        assert session.state is SessionState.IDLE
