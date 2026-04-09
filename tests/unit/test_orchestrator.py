"""Tests for SessionOrchestrator lifecycle and behavior."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from syncfield.clock import SessionClock
from syncfield.orchestrator import SessionOrchestrator
from syncfield.testing import FakeStream
from syncfield.tone import ChirpPlayer, ChirpSpec, SyncToneConfig
from syncfield.types import ChirpEmission, HealthEventKind, SessionState


def _mk_emission(
    software_ns: int = 1_000_000,
    hardware_ns: int | None = None,
    source: str = "software_fallback",
) -> ChirpEmission:
    """Build a ``ChirpEmission`` for tests that mock ``ChirpPlayer.play``."""
    return ChirpEmission(
        software_ns=software_ns,
        hardware_ns=hardware_ns,
        source=source,  # type: ignore[arg-type]
    )


def _mock_player() -> MagicMock:
    """Build a ``MagicMock`` spec'd on :class:`ChirpPlayer` that returns
    distinct :class:`ChirpEmission` values for successive ``play`` calls.

    Most tests don't care about the exact numeric values as long as they
    differ so ``chirp_stop_ns > chirp_start_ns`` assertions hold.
    """
    player = MagicMock(spec=ChirpPlayer)
    player.is_silent.return_value = False
    player.play.side_effect = [
        _mk_emission(software_ns=1_000_000),
        _mk_emission(software_ns=2_000_000),
    ]
    return player


def _fast_chirp_config() -> SyncToneConfig:
    """Very short chirp + margins — keeps orchestrator tests snappy."""
    return SyncToneConfig(
        enabled=True,
        start_chirp=ChirpSpec(400, 2500, 10, 0.8, 2),
        stop_chirp=ChirpSpec(2500, 400, 10, 0.8, 2),
        post_start_stabilization_ms=5,
        pre_stop_tail_margin_ms=5,
    )


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


class TestStop:
    def test_stop_transitions_to_stopped(self, tmp_path):
        session = _session(tmp_path)
        session.add(FakeStream("a"))
        session.start()
        report = session.stop()
        assert session.state is SessionState.STOPPED
        assert report.host_id == "rig_01"

    def test_stop_calls_stop_on_every_stream(self, tmp_path):
        session = _session(tmp_path)
        fs1 = FakeStream("a")
        fs2 = FakeStream("b")
        session.add(fs1)
        session.add(fs2)
        session.start()
        session.stop()
        assert fs1.stop_calls == 1
        assert fs2.stop_calls == 1

    def test_stop_collects_finalization_reports(self, tmp_path):
        session = _session(tmp_path)
        fs1 = FakeStream("a")
        fs2 = FakeStream("b")
        session.add(fs1)
        session.add(fs2)
        session.start()
        fs1.push_sample(0, 100)
        fs1.push_sample(1, 200)
        report = session.stop()
        by_id = {r.stream_id: r for r in report.finalizations}
        assert by_id["a"].frame_count == 2
        assert by_id["a"].first_sample_at_ns == 100
        assert by_id["a"].last_sample_at_ns == 200
        assert by_id["b"].frame_count == 0

    def test_stop_writes_sync_point_json(self, tmp_path):
        session = _session(tmp_path)
        session.add(FakeStream("a"))
        session.start()
        session.stop()
        sp = json.loads((tmp_path / "sync_point.json").read_text())
        assert sp["host_id"] == "rig_01"
        assert "monotonic_ns" in sp
        # Silent mode → no chirp fields
        assert "chirp_start_ns" not in sp

    def test_stop_writes_manifest_with_capabilities(self, tmp_path):
        session = _session(tmp_path)
        session.add(FakeStream("a", provides_audio_track=True))
        session.start()
        session.stop()
        m = json.loads((tmp_path / "manifest.json").read_text())
        assert m["host_id"] == "rig_01"
        assert "a" in m["streams"]
        assert m["streams"]["a"]["capabilities"]["provides_audio_track"] is True
        assert m["streams"]["a"]["status"] == "completed"
        assert m["streams"]["a"]["frame_count"] == 0

    def test_stop_requires_recording_state(self, tmp_path):
        session = _session(tmp_path)
        with pytest.raises(RuntimeError, match="stop.*idle"):
            session.stop()

    def test_failing_stream_does_not_block_other_stops(self, tmp_path):
        session = _session(tmp_path)
        session.add(FakeStream("good"))
        session.add(FakeStream("bad", fail_on_stop=True))
        session.start()
        report = session.stop()
        by_id = {r.stream_id: r for r in report.finalizations}
        assert by_id["good"].status == "completed"
        assert by_id["bad"].status == "failed"
        # Session still reaches STOPPED state — stop() is best-effort
        assert session.state is SessionState.STOPPED


class TestChirpIntegration:
    def test_chirp_skipped_when_no_audio_capable_stream(self, tmp_path, caplog):
        player = _mock_player()
        session = SessionOrchestrator(
            host_id="h",
            output_dir=tmp_path,
            sync_tone=SyncToneConfig.default(),
            chirp_player=player,
        )
        session.add(FakeStream("a", provides_audio_track=False))
        with caplog.at_level("INFO", logger="syncfield.orchestrator"):
            session.start()
        session.stop()
        player.play.assert_not_called()
        assert "cannot participate" in caplog.text.lower()

    def test_chirp_played_when_audio_capable_stream_exists(self, tmp_path):
        player = _mock_player()
        session = SessionOrchestrator(
            host_id="h",
            output_dir=tmp_path,
            sync_tone=_fast_chirp_config(),
            chirp_player=player,
        )
        session.add(FakeStream("a", provides_audio_track=True))
        session.start()
        session.stop()
        assert player.play.call_count == 2  # start + stop chirp

    def test_silent_tone_never_plays_chirp(self, tmp_path):
        player = _mock_player()
        session = SessionOrchestrator(
            host_id="h",
            output_dir=tmp_path,
            sync_tone=SyncToneConfig.silent(),
            chirp_player=player,
        )
        session.add(FakeStream("a", provides_audio_track=True))
        session.start()
        session.stop()
        player.play.assert_not_called()

    def test_chirp_fields_written_to_sync_point_json(self, tmp_path):
        player = _mock_player()
        session = SessionOrchestrator(
            host_id="h",
            output_dir=tmp_path,
            sync_tone=_fast_chirp_config(),
            chirp_player=player,
        )
        session.add(FakeStream("a", provides_audio_track=True))
        session.start()
        session.stop()
        sp = json.loads((tmp_path / "sync_point.json").read_text())
        assert "chirp_start_ns" in sp
        assert "chirp_stop_ns" in sp
        assert sp["chirp_start_ns"] > 0
        assert sp["chirp_stop_ns"] > sp["chirp_start_ns"]
        assert sp["chirp_spec"]["from_hz"] == 400

    def test_session_report_carries_chirp_timestamps(self, tmp_path):
        player = _mock_player()
        session = SessionOrchestrator(
            host_id="h",
            output_dir=tmp_path,
            sync_tone=_fast_chirp_config(),
            chirp_player=player,
        )
        session.add(FakeStream("a", provides_audio_track=True))
        session.start()
        report = session.stop()
        assert report.chirp_start_ns is not None
        assert report.chirp_stop_ns is not None


class TestChirpEmissionPropagation:
    def test_hardware_emission_surfaces_in_session_report(self, tmp_path):
        player = MagicMock(spec=ChirpPlayer)
        player.is_silent.return_value = False
        player.play.side_effect = [
            ChirpEmission(software_ns=100, hardware_ns=500, source="hardware"),
            ChirpEmission(software_ns=200, hardware_ns=700, source="hardware"),
        ]
        session = SessionOrchestrator(
            host_id="h",
            output_dir=tmp_path,
            sync_tone=_fast_chirp_config(),
            chirp_player=player,
        )
        session.add(FakeStream("a", provides_audio_track=True))
        session.start()
        report = session.stop()

        assert report.chirp_start_ns == 500
        assert report.chirp_stop_ns == 700
        assert report.chirp_start_source == "hardware"
        assert report.chirp_stop_source == "hardware"

        sp = json.loads((tmp_path / "sync_point.json").read_text())
        assert sp["chirp_start_source"] == "hardware"
        assert sp["chirp_stop_source"] == "hardware"
        assert sp["chirp_start_ns"] == 500
        assert sp["chirp_stop_ns"] == 700

    def test_software_fallback_emission_surfaces_in_report(self, tmp_path):
        player = MagicMock(spec=ChirpPlayer)
        player.is_silent.return_value = False
        player.play.side_effect = [
            ChirpEmission(
                software_ns=1_000, hardware_ns=None, source="software_fallback"
            ),
            ChirpEmission(
                software_ns=2_000, hardware_ns=None, source="software_fallback"
            ),
        ]
        session = SessionOrchestrator(
            host_id="h",
            output_dir=tmp_path,
            sync_tone=_fast_chirp_config(),
            chirp_player=player,
        )
        session.add(FakeStream("a", provides_audio_track=True))
        session.start()
        report = session.stop()

        assert report.chirp_start_ns == 1_000
        assert report.chirp_stop_ns == 2_000
        assert report.chirp_start_source == "software_fallback"
        assert report.chirp_stop_source == "software_fallback"


class TestSessionLog:
    def test_session_log_captures_state_transitions(self, tmp_path):
        session = _session(tmp_path)
        session.add(FakeStream("a"))
        session.start()
        session.stop()

        log_path = tmp_path / "session_log.jsonl"
        assert log_path.exists()
        lines = [json.loads(l) for l in log_path.read_text().strip().split("\n")]
        transitions = [l for l in lines if l["kind"] == "state_transition"]
        edges = {(t["from"], t["to"]) for t in transitions}
        assert ("idle", "preparing") in edges
        assert ("preparing", "recording") in edges
        assert ("recording", "stopping") in edges
        assert ("stopping", "stopped") in edges

    def test_session_log_flushes_during_recording(self, tmp_path):
        """A crash between start() and stop() must leave a readable log."""
        session = _session(tmp_path)
        session.add(FakeStream("a"))
        session.start()
        # Simulate "read the log while still RECORDING"
        content = (tmp_path / "session_log.jsonl").read_text()
        assert "preparing" in content
        assert "recording" in content
        session.stop()

    def test_rollback_is_logged(self, tmp_path):
        session = _session(tmp_path)
        session.add(FakeStream("a"))
        session.add(FakeStream("b", fail_on_start=True))
        with pytest.raises(RuntimeError):
            session.start()

        log_path = tmp_path / "session_log.jsonl"
        assert log_path.exists()
        lines = [json.loads(l) for l in log_path.read_text().strip().split("\n")]
        assert any(l["kind"] == "rollback" for l in lines)


class TestHealthRouting:
    def test_stream_health_events_routed_to_session_log(self, tmp_path):
        session = _session(tmp_path)
        fs = FakeStream("a")
        session.add(fs)
        session.start()
        fs.push_health(HealthEventKind.DROP, at_ns=500, detail="buffer full")
        fs.push_health(HealthEventKind.RECONNECT, at_ns=600)
        session.stop()

        lines = [
            json.loads(l)
            for l in (tmp_path / "session_log.jsonl").read_text().strip().split("\n")
        ]
        health_lines = [l for l in lines if l["kind"] == "health"]
        assert len(health_lines) == 2
        assert health_lines[0]["stream_id"] == "a"
        assert health_lines[0]["health_kind"] == "drop"
        assert health_lines[0]["detail"] == "buffer full"
        assert health_lines[1]["health_kind"] == "reconnect"

    def test_health_emitted_before_start_is_buffered_not_logged(self, tmp_path):
        """Before start(), the session log isn't open yet — health events
        must still reach the FinalizationReport via the StreamBase buffer.
        """
        session = _session(tmp_path)
        fs = FakeStream("a")
        session.add(fs)
        # Session log not yet open
        fs.push_health(HealthEventKind.WARNING, at_ns=1, detail="early")
        session.start()
        report = session.stop()

        final = next(f for f in report.finalizations if f.stream_id == "a")
        assert any(
            h.kind is HealthEventKind.WARNING and h.detail == "early"
            for h in final.health_events
        )
