"""Tests for SessionOrchestrator.set_chirp_mode + .chirp_mode property."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncfield.orchestrator import SessionOrchestrator, SessionState
from syncfield.testing import FakeStream
from syncfield.tone import SyncToneConfig, chirp_mode_of


def _session(tmp_path: Path, **kwargs) -> SessionOrchestrator:
    return SessionOrchestrator(
        host_id=kwargs.pop("host_id", "rig_01"),
        output_dir=tmp_path,
        **kwargs,
    )


class TestChirpModeClassification:
    def test_default_is_ultrasound(self):
        assert chirp_mode_of(SyncToneConfig.default()) == "ultrasound"

    def test_audible_preset(self):
        assert chirp_mode_of(SyncToneConfig.audible()) == "audible"

    def test_silent_preset(self):
        assert chirp_mode_of(SyncToneConfig.silent()) == "off"


class TestSetChirpMode:
    def test_default_mode_reflects_init_config(self, tmp_path: Path):
        session = _session(tmp_path, sync_tone=SyncToneConfig.default())
        assert session.chirp_mode == "ultrasound"

    def test_init_with_audible_reflects_in_property(self, tmp_path: Path):
        session = _session(tmp_path, sync_tone=SyncToneConfig.audible())
        assert session.chirp_mode == "audible"

    def test_init_with_silent_reflects_in_property(self, tmp_path: Path):
        session = _session(tmp_path, sync_tone=SyncToneConfig.silent())
        assert session.chirp_mode == "off"

    def test_switch_to_audible(self, tmp_path: Path):
        session = _session(tmp_path, sync_tone=SyncToneConfig.default())
        session.set_chirp_mode("audible")
        assert session.chirp_mode == "audible"
        assert session._sync_tone.enabled is True
        assert session._sync_tone.start_chirp.from_hz < 10_000

    def test_switch_to_off(self, tmp_path: Path):
        session = _session(tmp_path, sync_tone=SyncToneConfig.default())
        session.set_chirp_mode("off")
        assert session.chirp_mode == "off"
        assert session._sync_tone.enabled is False

    def test_switch_back_to_ultrasound(self, tmp_path: Path):
        session = _session(tmp_path, sync_tone=SyncToneConfig.audible())
        session.set_chirp_mode("ultrasound")
        assert session.chirp_mode == "ultrasound"
        assert session._sync_tone.enabled is True
        assert session._sync_tone.start_chirp.from_hz >= 10_000

    def test_invalid_mode_raises_value_error(self, tmp_path: Path):
        session = _session(tmp_path)
        with pytest.raises(ValueError, match="unknown chirp mode"):
            session.set_chirp_mode("loud")

    def test_rejected_during_recording(self, tmp_path: Path):
        session = _session(tmp_path, sync_tone=SyncToneConfig.silent())
        session.add(FakeStream("cam"))
        # Force the state into RECORDING without going through start() to
        # avoid touching real audio devices in this unit test.
        session._state = SessionState.RECORDING
        with pytest.raises(RuntimeError, match="cannot change chirp mode"):
            session.set_chirp_mode("audible")
        # Mode unchanged.
        assert session.chirp_mode == "off"

    def test_rejected_during_connecting(self, tmp_path: Path):
        session = _session(tmp_path, sync_tone=SyncToneConfig.default())
        session._state = SessionState.CONNECTING
        with pytest.raises(RuntimeError, match="cannot change chirp mode"):
            session.set_chirp_mode("off")

    def test_rejected_when_connected(self, tmp_path: Path):
        # CONNECTED is rejected to give a predictable contract:
        # configure chirp mode before connecting, or after stopping.
        # The frontend grays out the selector in CONNECTED to match.
        session = _session(tmp_path, sync_tone=SyncToneConfig.default())
        session._state = SessionState.CONNECTED
        with pytest.raises(RuntimeError, match="cannot change chirp mode"):
            session.set_chirp_mode("audible")
        assert session.chirp_mode == "ultrasound"

    def test_allowed_when_stopped(self, tmp_path: Path):
        session = _session(tmp_path, sync_tone=SyncToneConfig.default())
        session._state = SessionState.STOPPED
        session.set_chirp_mode("audible")
        assert session.chirp_mode == "audible"

    def test_rejected_during_preparing(self, tmp_path: Path):
        session = _session(tmp_path, sync_tone=SyncToneConfig.default())
        session._state = SessionState.PREPARING
        with pytest.raises(RuntimeError, match="cannot change chirp mode"):
            session.set_chirp_mode("audible")

    def test_rejected_during_countdown(self, tmp_path: Path):
        session = _session(tmp_path, sync_tone=SyncToneConfig.default())
        session._state = SessionState.COUNTDOWN
        with pytest.raises(RuntimeError, match="cannot change chirp mode"):
            session.set_chirp_mode("audible")

    def test_rejected_during_stopping(self, tmp_path: Path):
        session = _session(tmp_path, sync_tone=SyncToneConfig.default())
        session._state = SessionState.STOPPING
        with pytest.raises(RuntimeError, match="cannot change chirp mode"):
            session.set_chirp_mode("audible")


class TestOffModeKeepsCountdownTick:
    def test_silent_keeps_countdown_tick(self):
        cfg = SyncToneConfig.silent()
        assert cfg.enabled is False
        # The 3/2/1 countdown tick should still be configured so the
        # operator hears the recording about to start.
        assert cfg.countdown_tick is not None

    def test_make_off_mode_keeps_countdown_tick(self):
        from syncfield.tone import make_sync_tone_for_mode

        cfg = make_sync_tone_for_mode("off")
        assert cfg.enabled is False
        assert cfg.countdown_tick is not None
