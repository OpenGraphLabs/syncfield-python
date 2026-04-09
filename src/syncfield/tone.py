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
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from syncfield.types import ChirpSpec


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
#
# These spec values are ported directly from the egonaut production
# implementation (``EgonautMobile/SoundFeedbackModule.swift``) and have been
# validated against real iPhone microphones in field recording sessions.
# The rising-then-falling asymmetry is intentional: it lets the alignment
# core distinguish start chirps from stop chirps via cross-correlation.

_DEFAULT_START_CHIRP = ChirpSpec(
    from_hz=400, to_hz=2500, duration_ms=500, amplitude=0.8, envelope_ms=15
)
_DEFAULT_STOP_CHIRP = ChirpSpec(
    from_hz=2500, to_hz=400, duration_ms=500, amplitude=0.8, envelope_ms=15
)


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


def _float_to_int16(sample: float) -> int:
    """Clamp a float to ``[-1, 1]`` and scale to int16 range."""
    clamped = max(-1.0, min(1.0, sample))
    return int(round(clamped * 32767))


def write_chirp_wav(
    spec: ChirpSpec,
    path: Path | str,
    sample_rate: int = 44100,
) -> Path:
    """Write a chirp to a 16-bit mono PCM ``.wav`` file.

    Used by playback backends and for debugging chirp signals. Samples are
    clamped to ``[-1, 1]`` before int16 conversion so amplitude overflows
    never corrupt the output.

    Args:
        spec: Chirp parameters.
        path: Output file path (``str`` or :class:`~pathlib.Path`).
        sample_rate: Sample rate in Hz. Default ``44100``.

    Returns:
        The path that was written, as a :class:`~pathlib.Path`.
    """
    out_path = Path(path)
    samples = generate_chirp_samples(spec, sample_rate)
    int16_samples = [_float_to_int16(s) for s in samples]
    frames = struct.pack(f"<{len(int16_samples)}h", *int16_samples)
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(sample_rate)
        w.writeframes(frames)
    return out_path


@dataclass(frozen=True)
class SyncToneConfig:
    """Configuration for automatic audio sync chirp injection.

    Controls whether the :class:`~syncfield.orchestrator.SessionOrchestrator`
    plays sync chirps at session start and stop, the parameters of those
    chirps, and the timing margins around them so that each chirp is
    captured in the recording's audio track.

    Defaults are taken from the egonaut production implementation:

    - ``start_chirp``: 400 → 2500 Hz rising sweep
    - ``stop_chirp``: 2500 → 400 Hz falling sweep
    - ``duration_ms``: 500 ms each
    - ``amplitude``: 0.8 with a 15 ms cosine envelope
    - ``post_start_stabilization_ms``: 200 ms (let audio pipelines warm up)
    - ``pre_stop_tail_margin_ms``: 200 ms (let the chirp tail flush into WAV)

    Attributes:
        enabled: If ``False``, the orchestrator never plays a chirp and
            never writes chirp fields to ``sync_point.json``.
        start_chirp: Parameters for the chirp played right after all
            streams have started.
        stop_chirp: Parameters for the chirp played right before the
            orchestrator stops all streams.
        post_start_stabilization_ms: How long to wait after starting every
            stream before playing the start chirp.
        pre_stop_tail_margin_ms: Extra wait time (on top of the stop
            chirp's own duration) before stopping streams so the chirp
            tail is fully captured in any recording audio track.
    """

    enabled: bool = True
    start_chirp: ChirpSpec = field(default_factory=lambda: _DEFAULT_START_CHIRP)
    stop_chirp: ChirpSpec = field(default_factory=lambda: _DEFAULT_STOP_CHIRP)
    post_start_stabilization_ms: int = 200
    pre_stop_tail_margin_ms: int = 200

    @classmethod
    def default(cls) -> "SyncToneConfig":
        """Construct with all defaults (chirp enabled)."""
        return cls()

    @classmethod
    def silent(cls) -> "SyncToneConfig":
        """Construct with chirp disabled.

        Use for recording environments where audible chirps are
        unacceptable (quiet rooms, meetings) or for headless lab machines
        with no audio output path.
        """
        return cls(enabled=False)
