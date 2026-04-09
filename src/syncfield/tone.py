"""Sync tone generation, serialization, and playback.

Generates the linear FM chirp audio signal used by SyncField's
cross-correlation-based multi-host alignment. Chirp defaults (400↔2500 Hz
rising/falling, 500 ms, cosine envelope) are ported directly from the
egonaut production implementation (``EgonautMobile/SoundFeedbackModule.swift``)
which has been validated for reliable xcorr peaks across iPhone microphones
in real field recording conditions.

The synthesis path is pure standard library (``math`` only) so the core SDK
stays lightweight — no numpy dependency. Playback is optional and uses the
``sounddevice`` package when available, with a graceful silent fallback on
headless machines.
"""

from __future__ import annotations

import math
from typing import List

from syncfield.types import ChirpSpec


def generate_chirp_samples(spec: ChirpSpec, sample_rate: int = 44100) -> List[float]:
    """Generate mono PCM float samples for a linear FM chirp with cosine envelope.

    The instantaneous frequency sweeps linearly from ``spec.from_hz`` to
    ``spec.to_hz`` over ``spec.duration_ms``. A cosine (raised-cosine)
    envelope of length ``spec.envelope_ms`` is applied at attack and release.
    Amplitude is scaled by ``spec.amplitude`` (``0.0``–``1.0``).

    Mathematical form::

        f(t)      = f0 + (f1 - f0) * (t / T)
        phase(t)  = 2π · (f0·t + 0.5·k·t²),   k = (f1 - f0) / T
        envelope  = cosine fade of width ``envelope_ms`` at each end

    Args:
        spec: Chirp parameters.
        sample_rate: Output sample rate in Hz. Default ``44100``.

    Returns:
        Mono list of floats in ``[-amplitude, amplitude]``.
    """
    duration_s = spec.duration_ms / 1000.0
    total_samples = int(sample_rate * duration_s)
    if total_samples == 0 or spec.amplitude == 0.0:
        return [0.0] * total_samples

    f0 = float(spec.from_hz)
    f1 = float(spec.to_hz)
    sweep_rate = (f1 - f0) / duration_s  # Hz/s

    envelope_len = int(sample_rate * spec.envelope_ms / 1000.0)
    envelope_len = min(envelope_len, total_samples // 2)

    out: List[float] = [0.0] * total_samples
    for i in range(total_samples):
        t = i / sample_rate
        phase = 2.0 * math.pi * (f0 * t + 0.5 * sweep_rate * t * t)
        value = math.sin(phase)

        if envelope_len > 0:
            if i < envelope_len:
                env = 0.5 * (1.0 - math.cos(math.pi * i / envelope_len))
            elif i >= total_samples - envelope_len:
                tail = total_samples - 1 - i
                env = 0.5 * (1.0 - math.cos(math.pi * tail / envelope_len))
            else:
                env = 1.0
            value *= env

        out[i] = spec.amplitude * value

    return out
