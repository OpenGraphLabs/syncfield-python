"""Tests for sync tone generation, serialization, and playback."""

from __future__ import annotations

import dataclasses
import struct
import sys
import wave
from unittest.mock import MagicMock, patch

import pytest

from syncfield.tone import (
    ChirpPlayer,
    SilentChirpPlayer,
    SoundDeviceChirpPlayer,
    SyncToneConfig,
    create_default_player,
    generate_chirp_samples,
    write_chirp_wav,
)
from syncfield.types import ChirpSpec


SAMPLE_RATE = 44100


class TestGenerateChirpSamples:
    def test_length_matches_duration(self):
        spec = ChirpSpec(from_hz=400, to_hz=2500, duration_ms=500, amplitude=0.8, envelope_ms=15)
        samples = generate_chirp_samples(spec, sample_rate=SAMPLE_RATE)
        assert len(samples) == int(SAMPLE_RATE * 0.5)

    def test_amplitude_bounds(self):
        spec = ChirpSpec(from_hz=400, to_hz=2500, duration_ms=500, amplitude=0.8, envelope_ms=15)
        samples = generate_chirp_samples(spec, sample_rate=SAMPLE_RATE)
        peak = max(abs(s) for s in samples)
        assert peak <= 0.8 + 1e-9
        # Non-trivial signal somewhere in the middle
        assert peak > 0.5

    def test_envelope_ramps_from_and_to_zero(self):
        """Cosine envelope means the first and last samples are ~0."""
        spec = ChirpSpec(from_hz=400, to_hz=2500, duration_ms=500, amplitude=0.8, envelope_ms=15)
        samples = generate_chirp_samples(spec, sample_rate=SAMPLE_RATE)
        assert abs(samples[0]) < 0.01
        assert abs(samples[-1]) < 0.01

    def test_linear_frequency_sweep_zero_crossings(self):
        """For a 1000→3000 Hz linear sweep over 1 s the mean frequency is 2000 Hz,
        which produces roughly 4000 zero crossings. Allow ±5 %.
        """
        spec = ChirpSpec(from_hz=1000, to_hz=3000, duration_ms=1000, amplitude=1.0, envelope_ms=0)
        samples = generate_chirp_samples(spec, sample_rate=SAMPLE_RATE)
        zero_crossings = sum(
            1 for i in range(1, len(samples)) if samples[i - 1] * samples[i] < 0
        )
        assert 3800 < zero_crossings < 4200

    def test_silent_when_amplitude_zero(self):
        spec = ChirpSpec(400, 2500, 500, amplitude=0.0, envelope_ms=15)
        samples = generate_chirp_samples(spec, sample_rate=SAMPLE_RATE)
        assert all(s == 0.0 for s in samples)

    def test_empty_when_duration_zero(self):
        spec = ChirpSpec(400, 2500, 0, amplitude=0.8, envelope_ms=0)
        assert generate_chirp_samples(spec, sample_rate=SAMPLE_RATE) == []


class TestWriteChirpWav:
    def test_writes_valid_16bit_pcm_wav(self, tmp_path):
        spec = ChirpSpec(400, 2500, 500, 0.8, 15)
        out_path = tmp_path / "chirp.wav"
        write_chirp_wav(spec, out_path, sample_rate=SAMPLE_RATE)
        assert out_path.exists()
        with wave.open(str(out_path), "rb") as w:
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2  # 16-bit
            assert w.getframerate() == SAMPLE_RATE
            assert w.getnframes() == int(SAMPLE_RATE * 0.5)

    def test_samples_are_int16_and_nontrivial(self, tmp_path):
        # amplitude = 1.0 would overflow int16 if not clipped
        spec = ChirpSpec(400, 2500, 100, amplitude=1.0, envelope_ms=0)
        out_path = tmp_path / "chirp.wav"
        write_chirp_wav(spec, out_path, sample_rate=SAMPLE_RATE)
        with wave.open(str(out_path), "rb") as w:
            frames = w.readframes(w.getnframes())
        values = struct.unpack(f"<{len(frames) // 2}h", frames)
        assert all(-32768 <= v <= 32767 for v in values)
        assert max(abs(v) for v in values) > 20000

    def test_returns_path(self, tmp_path):
        out = tmp_path / "x.wav"
        result = write_chirp_wav(ChirpSpec(400, 500, 10, 0.5, 0), out)
        assert result == out


class TestSyncToneConfig:
    def test_default_uses_near_ultrasonic_band(self):
        cfg = SyncToneConfig.default()
        assert cfg.enabled is True
        # Start chirp rises 17 → 19 kHz (near-ultrasonic, AAC-safe, validated
        # inaudible to adults on Insta360 Go 3S + MacBook speakers).
        assert cfg.start_chirp.from_hz == 17000
        assert cfg.start_chirp.to_hz == 19000
        assert cfg.start_chirp.duration_ms == 500
        assert cfg.start_chirp.amplitude == 0.8
        assert cfg.start_chirp.envelope_ms == 15
        # Stop chirp is the reverse sweep (19 → 17 kHz) so the alignment
        # core can distinguish start from stop via xcorr sign.
        assert cfg.stop_chirp.from_hz == 19000
        assert cfg.stop_chirp.to_hz == 17000
        # Timing margins
        assert cfg.post_start_stabilization_ms == 200
        assert cfg.pre_stop_tail_margin_ms == 200

    def test_silent_factory_disables_playback(self):
        cfg = SyncToneConfig.silent()
        assert cfg.enabled is False

    def test_audible_factory_uses_legacy_400_2500_hz(self):
        cfg = SyncToneConfig.audible()
        assert cfg.enabled is True
        # Opt-in audible preset preserves the legacy 400-2500 Hz band that
        # was the default before the switch to near-ultrasonic.
        assert cfg.start_chirp.from_hz == 400
        assert cfg.start_chirp.to_hz == 2500
        assert cfg.stop_chirp.from_hz == 2500
        assert cfg.stop_chirp.to_hz == 400
        # Everything else matches default so downstream timing behaviour
        # is unchanged.
        assert cfg.start_chirp.duration_ms == 500
        assert cfg.start_chirp.amplitude == 0.8
        assert cfg.countdown_tick is not None
        assert cfg.post_start_stabilization_ms == 200

    def test_is_frozen(self):
        cfg = SyncToneConfig.default()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.enabled = False  # type: ignore[misc]

    def test_custom_values_round_trip(self):
        cfg = SyncToneConfig(
            enabled=True,
            start_chirp=ChirpSpec(100, 500, 100, 0.5, 5),
            stop_chirp=ChirpSpec(500, 100, 100, 0.5, 5),
            post_start_stabilization_ms=50,
            pre_stop_tail_margin_ms=50,
        )
        assert cfg.start_chirp.from_hz == 100
        assert cfg.post_start_stabilization_ms == 50


class TestSilentChirpPlayer:
    def test_play_returns_silent_emission(self):
        player: ChirpPlayer = SilentChirpPlayer()
        emission = player.play(ChirpSpec(400, 2500, 100, 0.5, 5))
        assert emission.source == "silent"
        assert emission.hardware_ns is None
        assert emission.software_ns > 0

    def test_is_silent_returns_true(self):
        assert SilentChirpPlayer().is_silent() is True

    def test_satisfies_chirp_player_protocol(self):
        assert isinstance(SilentChirpPlayer(), ChirpPlayer)


class TestSoundDeviceChirpPlayer:
    """Coarse integration-style tests that validate ``play()`` opens a
    ``sounddevice.OutputStream`` with an appropriate sample rate and
    returns a :class:`~syncfield.types.ChirpEmission`.

    Detailed hardware-timestamp capture behavior lives in
    ``test_chirp_emission.py`` (which drives a fake callback thread);
    these tests just guard the public surface.
    """

    def test_play_opens_outputstream_with_sample_rate(self):
        from types import SimpleNamespace

        class _Stub:
            def __init__(self, *, samplerate, channels, callback, finished_callback=None, **_):
                _Stub.last = self
                self.samplerate = samplerate
                self.channels = channels
                self.started = False
            def start(self):
                self.started = True
            def close(self):
                pass

        fake_sd = SimpleNamespace(
            OutputStream=_Stub,
            CallbackStop=type("CallbackStop", (Exception,), {}),
        )
        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            player = SoundDeviceChirpPlayer(sample_rate=SAMPLE_RATE)
            player._first_callback_timeout = 0.02  # type: ignore[attr-defined]
            emission = player.play(ChirpSpec(400, 2500, 100, 0.5, 5))
            assert _Stub.last.samplerate == SAMPLE_RATE
            assert _Stub.last.channels == 1
            assert _Stub.last.started is True
            # No callback fired → software fallback emission
            assert emission.source == "software_fallback"
            assert emission.software_ns > 0

    def test_play_does_not_call_sd_wait(self):
        """play() must NEVER call sd.wait() — the orchestrator owns all timing."""
        from types import SimpleNamespace

        wait_called = []

        class _Stub:
            def __init__(self, **_):
                pass
            def start(self):
                pass
            def close(self):
                pass

        def _wait(*a, **k):
            wait_called.append(True)

        fake_sd = SimpleNamespace(
            OutputStream=_Stub,
            CallbackStop=type("CallbackStop", (Exception,), {}),
            wait=_wait,
        )
        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            player = SoundDeviceChirpPlayer(sample_rate=SAMPLE_RATE)
            player._first_callback_timeout = 0.02  # type: ignore[attr-defined]
            player.play(ChirpSpec(400, 2500, 500, 0.8, 15))
            assert wait_called == []

    def test_is_silent_returns_false(self):
        fake_sd = MagicMock()
        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            assert SoundDeviceChirpPlayer().is_silent() is False


class TestCreateDefaultPlayer:
    def test_returns_sounddevice_backend_when_import_succeeds(self):
        fake_sd = MagicMock()
        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            assert isinstance(create_default_player(), SoundDeviceChirpPlayer)

    def test_returns_silent_backend_when_import_fails(self):
        # Patch sounddevice to None — import raises ImportError
        original = sys.modules.pop("sounddevice", None)
        try:
            with patch.dict(sys.modules, {"sounddevice": None}):
                assert isinstance(create_default_player(), SilentChirpPlayer)
        finally:
            if original is not None:
                sys.modules["sounddevice"] = original
