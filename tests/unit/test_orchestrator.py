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


class _DeviceKeyedFakeStream(FakeStream):
    """FakeStream variant that advertises a physical device key.

    Used to exercise :meth:`SessionOrchestrator.add` duplicate-device
    detection without needing real hardware adapters.
    """

    def __init__(self, id, device_key, **kwargs):
        super().__init__(id=id, **kwargs)
        self._device_key = device_key

    @property
    def device_key(self):
        return self._device_key


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

    def test_rejects_duplicate_physical_device(self, tmp_path):
        """Same (adapter_type, device_id) cannot be registered twice.

        Regression for the case where a user registered a camera in
        code and then ran Discover devices in the viewer — both paths
        succeeded and the session ended up with two cards for the
        same physical webcam.
        """
        session = _session(tmp_path)
        session.add(
            _DeviceKeyedFakeStream("mac_webcam", ("uvc_webcam", "0"))
        )
        with pytest.raises(ValueError, match="already registered as stream"):
            session.add(
                _DeviceKeyedFakeStream("macbook_pro", ("uvc_webcam", "0"))
            )

    def test_different_device_keys_are_not_duplicates(self, tmp_path):
        """Two streams on different device indices register cleanly."""
        session = _session(tmp_path)
        session.add(
            _DeviceKeyedFakeStream("mac_webcam", ("uvc_webcam", "0"))
        )
        session.add(
            _DeviceKeyedFakeStream("iphone", ("uvc_webcam", "1"))
        )
        assert len(session._streams) == 2

    def test_none_device_keys_fall_back_to_id_uniqueness(self, tmp_path):
        """Streams with no hardware identity (device_key == None) are
        only compared on stream id — two unique-id FakeStreams with
        no device_key must both register.
        """
        session = _session(tmp_path)
        session.add(FakeStream("a"))  # FakeStream default device_key == None
        session.add(FakeStream("b"))
        assert len(session._streams) == 2


class TestRemove:
    def test_remove_from_idle_state(self, tmp_path):
        session = _session(tmp_path)
        session.add(FakeStream("cam"))
        assert "cam" in session._streams
        session.remove("cam")
        assert "cam" not in session._streams
        assert session.state is SessionState.IDLE

    def test_remove_unknown_stream_raises_key_error(self, tmp_path):
        session = _session(tmp_path)
        with pytest.raises(KeyError, match="unknown stream id"):
            session.remove("ghost")

    def test_remove_rejected_during_recording(self, tmp_path):
        """Tearing a stream out of a live recording is not allowed."""
        session = _session(tmp_path)
        session.add(FakeStream("a"))
        session.add(FakeStream("b"))
        session.start()
        try:
            with pytest.raises(RuntimeError, match="remove.*requires"):
                session.remove("a")
            # Stream is still there after the failed remove.
            assert "a" in session._streams
        finally:
            session.stop()

    def test_remove_after_stop_allowed(self, tmp_path):
        """STOPPED is a valid state for removal — the session can be
        rebuilt with a different set of streams after one recording.
        """
        session = _session(tmp_path)
        session.add(FakeStream("a"))
        session.add(FakeStream("b"))
        session.start()
        session.stop()
        assert session.state is SessionState.STOPPED
        session.remove("a")
        assert "a" not in session._streams
        assert "b" in session._streams

    def test_remove_frees_device_key_for_re_add(self, tmp_path):
        """After removing a stream its device_key should free up so a
        fresh stream can grab the same hardware.
        """
        session = _session(tmp_path)
        session.add(
            _DeviceKeyedFakeStream("mac_webcam", ("uvc_webcam", "0"))
        )
        session.remove("mac_webcam")
        # Same device_key is no longer claimed.
        session.add(
            _DeviceKeyedFakeStream("mac_webcam_v2", ("uvc_webcam", "0"))
        )
        assert "mac_webcam_v2" in session._streams


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
        """A failure in ``prepare()`` happens during the connect phase,
        which runs all preparations before any stream starts recording.
        The rollback therefore calls ``disconnect()`` on streams that
        connected — and ``start()`` is never reached on any of them.

        This differs from the 0.1 behaviour where ``prepare()`` and
        ``start()`` interleaved per stream; the 0.2 orchestrator splits
        the two phases so all devices connect before any begin writing,
        matching the egonaut lab recorder's 2-phase model.
        """
        session = _session(tmp_path)
        good = FakeStream("a")
        bad = FakeStream("b", fail_on_prepare=True)
        session.add(good)
        session.add(bad)

        with pytest.raises(RuntimeError, match="fake failure in prepare"):
            session.start()

        # prepare() ran on both in the connect phase
        assert good.prepare_calls == 1
        assert bad.prepare_calls == 1
        # start_recording() was never invoked because the connect phase failed
        assert good.start_calls == 0
        assert bad.start_calls == 0
        # Rollback returned the auto-connected session to IDLE
        assert session.state is SessionState.IDLE


class TestFourPhaseLifecycle:
    """Cover the 0.2 explicit ``connect → start → stop → disconnect`` path.

    The legacy one-shot ``start() / stop()`` path is still exercised by
    :class:`TestStartHappyPath` and :class:`TestStop`. This class pins
    the newer semantics down:

    * ``connect()`` transitions ``IDLE → CONNECTING → CONNECTED`` and
      calls each stream's ``prepare()`` and ``connect()`` methods.
    * ``start(countdown_s=0)`` walks ``CONNECTED → PREPARING →
      COUNTDOWN → RECORDING`` and calls ``start_recording()`` on
      every stream (which routes to ``start()`` on a legacy
      :class:`FakeStream`).
    * ``stop()`` returns to ``CONNECTED`` (not ``STOPPED``) so the
      operator can record another episode without reopening devices.
    * ``disconnect()`` brings the session back to ``IDLE``.
    """

    def test_connect_start_stop_disconnect_happy_path(self, tmp_path):
        session = _session(tmp_path)
        fs = FakeStream("cam")
        session.add(fs)

        session.connect()
        assert session.state is SessionState.CONNECTED
        assert fs.prepare_calls == 1

        session.start(countdown_s=0)
        assert session.state is SessionState.RECORDING
        assert fs.start_calls == 1  # start_recording() → legacy start()

        report = session.stop()
        # Explicit-connect path stays in CONNECTED after stop so the
        # operator can record the next episode immediately.
        assert session.state is SessionState.CONNECTED
        assert fs.stop_calls == 1  # stop_recording() → legacy stop()
        assert report.host_id == "rig_01"

        session.disconnect()
        assert session.state is SessionState.IDLE

    def test_start_from_connected_does_not_auto_disconnect_on_stop(self, tmp_path):
        """Explicit connect + stop leaves devices open; a new start works."""
        session = _session(tmp_path)
        fs = FakeStream("cam")
        session.add(fs)

        session.connect()
        session.start(countdown_s=0)
        session.stop()
        assert session.state is SessionState.CONNECTED

        # Second recording — no reconnect needed.
        session.start(countdown_s=0)
        assert session.state is SessionState.RECORDING
        # Stream saw two recording cycles (two start+stop pairs).
        assert fs.start_calls == 2
        session.stop()
        assert fs.stop_calls == 2
        session.disconnect()

    def test_countdown_tick_callback_fires(self, tmp_path):
        """The ``on_countdown_tick`` callback should fire for each second."""
        session = _session(tmp_path)
        session.add(FakeStream("cam"))
        session.connect()

        seen = []
        session.start(
            countdown_s=3,
            on_countdown_tick=lambda n: seen.append(n),
        )
        # Ticks go 3 → 2 → 1 in descending order
        assert seen == [3, 2, 1]
        session.stop()
        session.disconnect()


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


# ---------------------------------------------------------------------------
# Multi-host leader / follower role integration
# ---------------------------------------------------------------------------


class _FakeAdvertiser:
    """Stand-in for :class:`syncfield.multihost.SessionAdvertiser`."""

    instances: list["_FakeAdvertiser"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        self.status_calls: list[tuple[str, int | None]] = []
        _FakeAdvertiser.instances.append(self)

    @property
    def session_id(self) -> str:
        return self.kwargs["session_id"]

    def start(self) -> None:
        self.started = True

    def update_status(self, status: str, *, started_at_ns=None) -> None:
        self.status_calls.append((status, started_at_ns))

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    """Stand-in for :class:`syncfield.multihost.SessionBrowser`.

    Scripted by setting :attr:`wait_recording_result` (either a
    :class:`SessionAnnouncement` to return or an :class:`Exception` to
    raise) before ``start()`` is called on the orchestrator.
    """

    instances: list["_FakeBrowser"] = []

    wait_recording_result: object = None
    wait_stopped_result: object = None

    def __init__(self, session_id=None):
        self.session_id = session_id
        self.started = False
        self.closed = False
        self.wait_recording_calls: list[float] = []
        self.wait_stopped_calls: list[float] = []
        _FakeBrowser.instances.append(self)

    def start(self) -> None:
        self.started = True

    def wait_for_recording(self, timeout: float):
        self.wait_recording_calls.append(timeout)
        result = type(self).wait_recording_result
        if isinstance(result, Exception):
            raise result
        return result

    def wait_for_stopped(self, timeout: float):
        self.wait_stopped_calls.append(timeout)
        result = type(self).wait_stopped_result
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_multihost(monkeypatch):
    """Patch the orchestrator's SessionAdvertiser + SessionBrowser symbols."""
    _FakeAdvertiser.instances.clear()
    _FakeBrowser.instances.clear()
    _FakeBrowser.wait_recording_result = None
    _FakeBrowser.wait_stopped_result = None
    monkeypatch.setattr(
        "syncfield.orchestrator.SessionAdvertiser", _FakeAdvertiser
    )
    monkeypatch.setattr(
        "syncfield.orchestrator.SessionBrowser", _FakeBrowser
    )
    yield
    _FakeAdvertiser.instances.clear()
    _FakeBrowser.instances.clear()


class TestLeaderRoleIntegration:
    def test_start_constructs_and_starts_advertiser(self, tmp_path, fake_multihost):
        from syncfield.roles import LeaderRole

        session = SessionOrchestrator(
            host_id="leader_host",
            output_dir=tmp_path,
            sync_tone=SyncToneConfig.silent(),
            role=LeaderRole(session_id="amber-tiger-042"),
        )
        session.add(FakeStream("cam"))
        session.start()

        assert len(_FakeAdvertiser.instances) == 1
        adv = _FakeAdvertiser.instances[0]
        assert adv.kwargs["session_id"] == "amber-tiger-042"
        assert adv.kwargs["host_id"] == "leader_host"
        assert adv.kwargs["chirp_enabled"] is False  # silent config
        assert adv.started is True
        # One update to `recording` after streams start.
        assert adv.status_calls == [("recording", session._sync_point.monotonic_ns)]

        session.stop()
        # Second update: stopped. Then close.
        assert ("stopped", None) in adv.status_calls
        assert adv.closed is True

    def test_stop_returns_session_report_with_leader_metadata(
        self, tmp_path, fake_multihost
    ):
        from syncfield.roles import LeaderRole

        session = SessionOrchestrator(
            host_id="leader_host",
            output_dir=tmp_path,
            sync_tone=SyncToneConfig.silent(),
            role=LeaderRole(session_id="amber-tiger-042"),
        )
        session.add(FakeStream("cam"))
        session.start()
        report = session.stop()

        assert report.role == "leader"
        assert report.session_id == "amber-tiger-042"

    def test_manifest_and_sync_point_include_session_id(
        self, tmp_path, fake_multihost
    ):
        from syncfield.roles import LeaderRole

        session = SessionOrchestrator(
            host_id="leader_host",
            output_dir=tmp_path,
            sync_tone=SyncToneConfig.silent(),
            role=LeaderRole(session_id="amber-tiger-042"),
        )
        session.add(FakeStream("cam"))
        session.start()
        session.stop()

        sp = json.loads((tmp_path / "sync_point.json").read_text())
        mf = json.loads((tmp_path / "manifest.json").read_text())
        assert sp["session_id"] == "amber-tiger-042"
        assert sp["role"] == "leader"
        assert mf["session_id"] == "amber-tiger-042"
        assert mf["role"] == "leader"
        assert "leader_host_id" not in mf  # leader has no leader_host_id

    def test_auto_generates_session_id(self, tmp_path, fake_multihost):
        from syncfield.roles import LeaderRole

        role = LeaderRole()  # no session_id
        session = SessionOrchestrator(
            host_id="h",
            output_dir=tmp_path,
            sync_tone=SyncToneConfig.silent(),
            role=role,
        )
        session.add(FakeStream("cam"))
        session.start()
        session.stop()

        assert role.session_id is not None
        assert session.session_id == role.session_id

    def test_chirp_chirp_enabled_flag_matches_sync_tone(
        self, tmp_path, fake_multihost
    ):
        """Leader with chirps enabled must advertise chirp_enabled=True."""
        from syncfield.roles import LeaderRole

        player = _mock_player()
        session = SessionOrchestrator(
            host_id="leader_host",
            output_dir=tmp_path,
            sync_tone=_fast_chirp_config(),
            chirp_player=player,
            role=LeaderRole(session_id="amber-tiger-042"),
        )
        session.add(FakeStream("cam", provides_audio_track=True))
        session.start()
        session.stop()

        adv = _FakeAdvertiser.instances[0]
        assert adv.kwargs["chirp_enabled"] is True
        # Leader DID play both chirps (start + stop).
        assert player.play.call_count == 2


class TestFollowerRoleIntegration:
    def _leader_announcement(self) -> "SessionAnnouncement":
        from syncfield.multihost.types import SessionAnnouncement

        return SessionAnnouncement(
            session_id="amber-tiger-042",
            host_id="leader_host",
            status="recording",
            sdk_version="0.2.0",
            chirp_enabled=True,
            started_at_ns=1234,
        )

    def test_start_blocks_for_leader_then_proceeds(
        self, tmp_path, fake_multihost
    ):
        from syncfield.roles import FollowerRole

        _FakeBrowser.wait_recording_result = self._leader_announcement()

        session = SessionOrchestrator(
            host_id="follower_host",
            output_dir=tmp_path,
            sync_tone=SyncToneConfig.silent(),
            role=FollowerRole(session_id="amber-tiger-042"),
        )
        session.add(FakeStream("cam"))
        session.start()

        assert len(_FakeBrowser.instances) == 1
        browser = _FakeBrowser.instances[0]
        assert browser.session_id == "amber-tiger-042"
        assert browser.started is True
        assert browser.wait_recording_calls == [60.0]  # default timeout

        assert session.observed_leader is not None
        assert session.observed_leader.host_id == "leader_host"
        assert session.session_id == "amber-tiger-042"

        report = session.stop()
        assert report.role == "follower"
        assert report.session_id == "amber-tiger-042"
        assert browser.closed is True

    def test_follower_never_plays_chirp_even_with_audio_stream(
        self, tmp_path, fake_multihost
    ):
        from syncfield.roles import FollowerRole

        _FakeBrowser.wait_recording_result = self._leader_announcement()

        player = _mock_player()
        session = SessionOrchestrator(
            host_id="follower_host",
            output_dir=tmp_path,
            sync_tone=_fast_chirp_config(),
            chirp_player=player,
            role=FollowerRole(session_id="amber-tiger-042"),
        )
        # Audio-capable stream would normally trigger chirps — but the
        # follower role must override that.
        session.add(FakeStream("audio_cam", provides_audio_track=True))
        session.start()
        session.stop()

        player.play.assert_not_called()

    def test_follower_leader_timeout_propagates_and_cleans_up(
        self, tmp_path, fake_multihost
    ):
        from syncfield.roles import FollowerRole

        _FakeBrowser.wait_recording_result = TimeoutError("no leader")

        session = SessionOrchestrator(
            host_id="follower_host",
            output_dir=tmp_path,
            sync_tone=SyncToneConfig.silent(),
            role=FollowerRole(
                session_id="amber-tiger-042", leader_wait_timeout_sec=0.05
            ),
        )
        session.add(FakeStream("cam"))
        with pytest.raises(TimeoutError):
            session.start()

        assert session.state is SessionState.IDLE
        # Browser was cleaned up on failure.
        assert _FakeBrowser.instances[0].closed is True

    def test_manifest_records_leader_host_id(self, tmp_path, fake_multihost):
        from syncfield.roles import FollowerRole

        _FakeBrowser.wait_recording_result = self._leader_announcement()

        session = SessionOrchestrator(
            host_id="follower_host",
            output_dir=tmp_path,
            sync_tone=SyncToneConfig.silent(),
            role=FollowerRole(session_id="amber-tiger-042"),
        )
        session.add(FakeStream("cam"))
        session.start()
        session.stop()

        mf = json.loads((tmp_path / "manifest.json").read_text())
        assert mf["role"] == "follower"
        assert mf["session_id"] == "amber-tiger-042"
        assert mf["leader_host_id"] == "leader_host"

    def test_wait_for_leader_stopped_delegates_to_browser(
        self, tmp_path, fake_multihost
    ):
        from syncfield.multihost.types import SessionAnnouncement
        from syncfield.roles import FollowerRole

        _FakeBrowser.wait_recording_result = self._leader_announcement()
        _FakeBrowser.wait_stopped_result = SessionAnnouncement(
            session_id="amber-tiger-042",
            host_id="leader_host",
            status="stopped",
            sdk_version="0.2.0",
            chirp_enabled=True,
            started_at_ns=1234,
        )

        session = SessionOrchestrator(
            host_id="follower_host",
            output_dir=tmp_path,
            sync_tone=SyncToneConfig.silent(),
            role=FollowerRole(session_id="amber-tiger-042"),
        )
        session.add(FakeStream("cam"))
        session.start()
        observed_stop = session.wait_for_leader_stopped(timeout=1.5)
        assert observed_stop.status == "stopped"
        assert _FakeBrowser.instances[0].wait_stopped_calls == [1.5]
        session.stop()

    def test_wait_for_leader_stopped_requires_follower_role(self, tmp_path):
        """Calling on a single-host session must raise."""
        session = SessionOrchestrator(
            host_id="h",
            output_dir=tmp_path,
            sync_tone=SyncToneConfig.silent(),
        )
        session.add(FakeStream("cam"))
        session.start()
        with pytest.raises(RuntimeError, match="FollowerRole"):
            session.wait_for_leader_stopped()
        session.stop()

    def test_wait_for_leader_stopped_before_start_raises(
        self, tmp_path, fake_multihost
    ):
        from syncfield.roles import FollowerRole

        session = SessionOrchestrator(
            host_id="h",
            output_dir=tmp_path,
            sync_tone=SyncToneConfig.silent(),
            role=FollowerRole(session_id="amber-tiger-042"),
        )
        with pytest.raises(RuntimeError, match="start"):
            session.wait_for_leader_stopped()


class TestSessionLog:
    def test_session_log_captures_state_transitions(self, tmp_path):
        session = _session(tmp_path)
        session.add(FakeStream("a"))
        session.start(countdown_s=0)
        session.stop()

        log_path = tmp_path / "session_log.jsonl"
        assert log_path.exists()
        lines = [json.loads(l) for l in log_path.read_text().strip().split("\n")]
        transitions = [l for l in lines if l["kind"] == "state_transition"]
        edges = {(t["from"], t["to"]) for t in transitions}
        # 0.2 four-phase lifecycle: IDLE → CONNECTING → CONNECTED →
        # PREPARING → COUNTDOWN → RECORDING → STOPPING → STOPPED
        # (the auto-connect path used by the legacy one-shot
        # start()/stop() still lands in STOPPED at the end).
        assert ("idle", "connecting") in edges
        assert ("connecting", "connected") in edges
        assert ("connected", "preparing") in edges
        assert ("preparing", "countdown") in edges
        assert ("countdown", "recording") in edges
        assert ("recording", "stopping") in edges
        assert ("stopping", "stopped") in edges

    def test_session_log_flushes_during_recording(self, tmp_path):
        """A crash between start() and stop() must leave a readable log."""
        session = _session(tmp_path)
        session.add(FakeStream("a"))
        session.start(countdown_s=0)
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
