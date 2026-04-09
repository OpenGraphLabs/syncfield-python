"""Tests for sync tone generation, serialization, and playback."""

from __future__ import annotations

from syncfield.tone import generate_chirp_samples
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
