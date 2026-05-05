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

import logging
import math
import struct
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Literal, Optional, Protocol, runtime_checkable

from syncfield.types import ChirpEmission, ChirpSource, ChirpSpec

logger = logging.getLogger(__name__)


# numpy is an optional runtime dependency of SoundDeviceChirpPlayer (sounddevice
# itself needs it internally). Import it lazily here — but at module load time
# rather than inside ``play()`` — so that test fixtures which patch
# ``sys.modules["sounddevice"]`` don't accidentally trigger numpy's C-extension
# one-time initialization failure inside a patch.dict block.
try:
    import numpy as _np  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on machines without numpy
    _np = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
#
# Start/stop chirps live in the 17–19 kHz near-ultrasonic band. Validated
# end-to-end against an Insta360 Go 3S (mic + AAC encoder) and a MacBook
# built-in speaker: the 17–19 kHz chirp survives the recording path with
# ~+20 dB SNR, well above the event-detector's 5× prominence floor used
# by the syncfield sync API. A higher 19–21 kHz candidate was also tested
# and found dead at the other end — either the MacBook tweeter cannot
# reach that band cleanly or the AAC encoder drops it. 17–19 kHz is the
# documented sweet spot: nearly inaudible to adults (hearing typically
# rolls off above 15–16 kHz), robust across the audio pipeline, and wide
# enough (2 kHz bandwidth × 500 ms) for sub-millisecond cross-correlation
# anchoring.
#
# The rising-then-falling asymmetry between start and stop is preserved
# so the alignment core can still distinguish the two via chirp direction.

_DEFAULT_START_CHIRP = ChirpSpec(
    from_hz=17000, to_hz=19000, duration_ms=500, amplitude=0.8, envelope_ms=15
)
_DEFAULT_STOP_CHIRP = ChirpSpec(
    from_hz=19000, to_hz=17000, duration_ms=500, amplitude=0.8, envelope_ms=15
)

# Audible (400-2500 Hz) variant — kept available as an opt-in preset. Use
# cases: (i) cameras with 16 kHz-or-lower audio cutoff where ultrasonic
# doesn't survive, (ii) debugging scenarios where the operator wants to
# hear the chirps fire, (iii) hearing-impaired workflows that rely on a
# visible/audible cue. These values are the egonaut-validated defaults
# we used before switching to the near-ultrasonic band.
_AUDIBLE_START_CHIRP = ChirpSpec(
    from_hz=400, to_hz=2500, duration_ms=500, amplitude=0.8, envelope_ms=15
)
_AUDIBLE_STOP_CHIRP = ChirpSpec(
    from_hz=2500, to_hz=400, duration_ms=500, amplitude=0.8, envelope_ms=15
)

# Countdown tick — a short flat-frequency beep played once per countdown
# second by :meth:`SessionOrchestrator.start`. The tone is C6 (1046.5 Hz)
# for 100 ms with a 10 ms envelope, which reads as a clean digital "tick"
# on MacBook speakers without being jarring. The operator hears
# beep · beep · beep before the start chirp sweeps in.
_DEFAULT_COUNTDOWN_TICK = ChirpSpec(
    from_hz=1047, to_hz=1047, duration_ms=100, amplitude=0.6, envelope_ms=10
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
        countdown_tick: Optional short beep played once per second
            during the ``COUNTDOWN`` phase. Defaults to a 100 ms C6
            tick so the operator hears ``beep · beep · beep`` before
            the start chirp sweeps in. Set to ``None`` to silence the
            countdown while keeping the start/stop chirps audible.
        post_start_stabilization_ms: How long to wait after starting every
            stream before playing the start chirp.
        pre_stop_tail_margin_ms: Extra wait time (on top of the stop
            chirp's own duration) before stopping streams so the chirp
            tail is fully captured in any recording audio track.
        suppress_host_audio: When ``True``, the
            :class:`~syncfield.orchestrator.SessionOrchestrator` MUST NOT
            auto-register a ``host_audio`` stream, even when a microphone
            is detected.  Automatically set by :meth:`silent` so that
            "silent mode" truly produces no audio-related streams.

    SDK contract for GUI consumers (see syncfield-sensor-onboarding-enhancements §5):

    4. **SyncToneConfig.silent() MUST NOT register a host_audio stream.**
       When :meth:`silent` is used, the orchestrator MUST skip auto-injection
       of :class:`~syncfield.adapters.host_audio.HostAudioStream`.  This is
       enforced via ``suppress_host_audio=True`` on the config object, which
       the orchestrator reads in both its pre-register and inject helpers.
       Callers who construct ``SyncToneConfig(enabled=False)`` directly retain
       the old behaviour (host_audio may still be injected) to preserve
       backward compatibility.
    """

    enabled: bool = True
    start_chirp: ChirpSpec = field(default_factory=lambda: _DEFAULT_START_CHIRP)
    stop_chirp: ChirpSpec = field(default_factory=lambda: _DEFAULT_STOP_CHIRP)
    countdown_tick: Optional[ChirpSpec] = field(
        default_factory=lambda: _DEFAULT_COUNTDOWN_TICK
    )
    post_start_stabilization_ms: int = 200
    pre_stop_tail_margin_ms: int = 200
    #: Set by :meth:`silent` to suppress host_audio auto-injection (Contract 4).
    suppress_host_audio: bool = False

    @classmethod
    def default(cls) -> "SyncToneConfig":
        """Construct with all defaults (near-ultrasonic chirp enabled).

        Uses the 17–19 kHz band start/stop chirps — nearly inaudible to
        adult ears but reliably captured by consumer camera mics through
        typical AAC recording pipelines. Prefer this for production.
        """
        return cls()

    @classmethod
    def audible(cls) -> "SyncToneConfig":
        """Construct with the legacy audible 400–2500 Hz start/stop chirps.

        Opt-in preset for cases where the near-ultrasonic default is not
        appropriate:

          - cameras/codecs with an audio cutoff below ~16 kHz where the
            ultrasonic chirp does not survive the recording pipeline,
          - debugging scenarios where an operator wants to hear that the
            chirps actually fired,
          - demos / workflows where a human-audible cue is required.

        All other parameters (duration, envelope, timing margins, countdown
        tick) match the default.
        """
        return cls(
            start_chirp=_AUDIBLE_START_CHIRP,
            stop_chirp=_AUDIBLE_STOP_CHIRP,
        )

    @classmethod
    def silent(cls) -> "SyncToneConfig":
        """Construct with the start/stop chirps disabled and host_audio suppressed.

        **SDK contract (Contract 4) — silent() MUST NOT register host_audio.**
        This factory sets ``suppress_host_audio=True`` so the
        :class:`~syncfield.orchestrator.SessionOrchestrator` MUST skip
        auto-injection of
        :class:`~syncfield.adapters.host_audio.HostAudioStream`.  In a
        truly silent session there is no acoustic sync path, so capturing
        a microphone track would produce a ghost ``host_audio`` stream
        that serves no purpose and confuses GUI users.

        Use for recording environments where the start/stop chirp
        itself is unacceptable (clinical, audio-sensitive subjects)
        but the operator still benefits from a 3/2/1 audible cue.
        """
        return cls(enabled=False, suppress_host_audio=True)


# ---------------------------------------------------------------------------
# Mode classification (used by viewer + control plane)
# ---------------------------------------------------------------------------

ChirpMode = Literal["ultrasound", "audible", "off"]


def chirp_mode_of(cfg: SyncToneConfig) -> ChirpMode:
    """Classify a :class:`SyncToneConfig` into one of three named modes.

    The classification keys off chirp enablement and start-chirp band:

    - ``"off"`` — ``enabled=False`` (no chirp played)
    - ``"ultrasound"`` — start chirp begins above ~10 kHz (the default
      17–19 kHz preset and any custom near-ultrasonic spec land here)
    - ``"audible"`` — start chirp begins below ~10 kHz (legacy
      400–2500 Hz preset and any custom audible spec)

    The 10 kHz boundary mirrors the practical split between "human
    perceives as chirp" and "human barely notices" — the actual default
    is much higher (17 kHz), so any reasonable user configuration falls
    cleanly on one side.
    """
    if not cfg.enabled:
        return "off"
    if cfg.start_chirp.from_hz >= 10_000:
        return "ultrasound"
    return "audible"


def make_sync_tone_for_mode(mode: ChirpMode) -> SyncToneConfig:
    """Construct the canonical :class:`SyncToneConfig` for a named mode.

    Inverse of :func:`chirp_mode_of` for the three preset modes:

    - ``"ultrasound"`` → :meth:`SyncToneConfig.default`
    - ``"audible"`` → :meth:`SyncToneConfig.audible`
    - ``"off"`` → :meth:`SyncToneConfig.silent`

    Raises:
        ValueError: If ``mode`` is not one of the three known values.
    """
    if mode == "ultrasound":
        return SyncToneConfig.default()
    if mode == "audible":
        return SyncToneConfig.audible()
    if mode == "off":
        return SyncToneConfig.silent()
    raise ValueError(
        f"unknown chirp mode {mode!r}; expected 'ultrasound', 'audible', or 'off'"
    )


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


@runtime_checkable
class ChirpPlayer(Protocol):
    """Protocol for playing a chirp to the system audio output.

    Implementations must be **non-blocking** for the full chirp duration:
    ``play()`` may briefly block waiting for the audio backend's first
    callback so it can capture a hardware DAC timestamp (typically a few
    milliseconds), but must never block for the entire chirp. The
    orchestrator handles all timing margins around the chirp.

    Returns a :class:`~syncfield.types.ChirpEmission` so callers can
    persist both the software send time and the best-available hardware
    presentation time with each session — this is the foundation of
    SyncField's chirp-anchored multi-host synchronization.
    """

    def play(self, spec: ChirpSpec) -> ChirpEmission:
        """Schedule playback and return the emission record."""
        ...

    def is_silent(self) -> bool:
        """True if this player produces no actual audio output."""
        ...


class SilentChirpPlayer:
    """No-op player used when ``sounddevice`` is unavailable or disabled.

    Emits an INFO log line on every ``play()`` so callers can see that a
    chirp was requested but not produced. Used automatically on headless
    lab machines where :func:`create_default_player` cannot import
    ``sounddevice``. Returns a :class:`ChirpEmission` tagged ``"silent"``
    so downstream sync tooling can distinguish "no audio path" from
    "tried to play but the backend had no DAC timestamp".
    """

    def play(self, spec: ChirpSpec) -> ChirpEmission:
        logger.info(
            "SilentChirpPlayer.play(%s): chirp skipped (no audio output)", spec
        )
        return ChirpEmission(
            software_ns=time.monotonic_ns(),
            hardware_ns=None,
            source="silent",
        )

    def is_silent(self) -> bool:
        return True


@dataclass
class _ChirpPlaybackState:
    """Shared state between :meth:`SoundDeviceChirpPlayer.play` and the
    PortAudio callback thread.

    Attributes:
        position: Index of the next sample to copy into ``outdata``.
        hardware_ns: Hardware DAC timestamp captured on first callback,
            or ``None`` if the backend did not expose DAC time.
        source: Provenance tag set on first callback.
        first_callback: Event set as soon as the first callback runs,
            unblocking :meth:`play`.
    """

    position: int = 0
    hardware_ns: Optional[int] = None
    source: ChirpSource = "software_fallback"
    first_callback: threading.Event = field(default_factory=threading.Event)


class SoundDeviceChirpPlayer:
    """Plays chirps via ``sounddevice`` with hardware DAC timestamp capture.

    On the first audio callback after :meth:`sounddevice.OutputStream.start`,
    PortAudio hands us a ``time_info`` struct whose
    ``outputBufferDacTime`` is the stream time at which the first sample
    in the buffer will be clocked out of the DAC. We sample
    ``time.monotonic_ns()`` inside the same callback and compute::

        hardware_ns = monotonic_at_callback
                      + (dac_time - current_time) * 1e9

    :meth:`play` briefly blocks (default 100 ms) waiting for that first
    callback so the returned :class:`ChirpEmission` can carry the
    hardware timestamp. If PortAudio does not expose DAC time on the
    current backend (``dac_time == current_time``) or the callback does
    not fire within the timeout, the player falls back to the software
    timestamp captured before ``stream.start()`` and tags the emission
    as ``"software_fallback"``.

    Active streams are pinned on :attr:`_active_streams` until their
    ``finished_callback`` fires so Python GC cannot tear the audio
    thread down while the chirp is still playing.

    ``sounddevice`` is an optional dependency
    (``pip install syncfield[audio]``). Prefer :func:`create_default_player`
    over direct instantiation — it chooses this backend when
    ``sounddevice`` imports successfully and falls back to
    :class:`SilentChirpPlayer` otherwise.

    Args:
        sample_rate: Sample rate used for sample synthesis and the
            PortAudio stream. Default ``44100``.
    """

    #: Default wait for the first callback before falling back to software
    #: timestamp. 100 ms comfortably covers default PortAudio buffer
    #: latencies on macOS/Linux/Windows (typical: 5–30 ms).
    DEFAULT_FIRST_CALLBACK_TIMEOUT_SEC = 0.1

    def __init__(self, sample_rate: int = 44100) -> None:
        self._sample_rate = sample_rate
        self._active_streams: List[Any] = []
        self._streams_lock = threading.Lock()
        self._first_callback_timeout = self.DEFAULT_FIRST_CALLBACK_TIMEOUT_SEC

    def play(self, spec: ChirpSpec) -> ChirpEmission:
        import sounddevice as sd  # type: ignore[import-not-found]

        samples = generate_chirp_samples(spec, sample_rate=self._sample_rate)
        buffer: Any
        if _np is not None:
            buffer = _np.asarray(samples, dtype=_np.float32)
        else:
            buffer = samples  # pragma: no cover - tested indirectly via fake sd
        total = len(buffer)

        state = _ChirpPlaybackState()

        def callback(outdata: Any, frames: int, time_info: Any, status: Any) -> None:
            if state.position == 0:
                mono_ns = time.monotonic_ns()
                try:
                    dac_time = float(time_info.outputBufferDacTime)
                    cur_time = float(time_info.currentTime)
                except (AttributeError, TypeError, ValueError):
                    dac_time = cur_time = 0.0
                if dac_time > cur_time:
                    offset_ns = int(
                        round((dac_time - cur_time) * 1_000_000_000)
                    )
                    state.hardware_ns = mono_ns + offset_ns
                    state.source = "hardware"
                else:
                    state.hardware_ns = None
                    state.source = "software_fallback"
                state.first_callback.set()

            end = min(state.position + frames, total)
            n = end - state.position
            if n > 0:
                outdata[:n, 0] = buffer[state.position:end]
            if n < frames:
                outdata[n:, 0] = 0.0
                state.position = total
                raise sd.CallbackStop
            state.position = end

        def finished_cb() -> None:
            self._drop_stream(stream)

        software_ns = time.monotonic_ns()
        stream = sd.OutputStream(
            samplerate=self._sample_rate,
            channels=1,
            callback=callback,
            finished_callback=finished_cb,
        )
        with self._streams_lock:
            self._active_streams.append(stream)
        try:
            stream.start()
        except Exception:
            self._drop_stream(stream)
            raise

        got_first = state.first_callback.wait(self._first_callback_timeout)
        if not got_first:
            return ChirpEmission(
                software_ns=software_ns,
                hardware_ns=None,
                source="software_fallback",
            )
        return ChirpEmission(
            software_ns=software_ns,
            hardware_ns=state.hardware_ns,
            source=state.source,
        )

    def is_silent(self) -> bool:
        return False

    def close(self) -> None:
        """Force-close any streams still pinned in the active list.

        Streams normally evict themselves via ``finished_callback`` when
        playback ends naturally. This method exists for tests and
        shutdown paths that need to force cleanup without waiting for
        the audio thread to drain.
        """
        with self._streams_lock:
            streams = list(self._active_streams)
            self._active_streams.clear()
        for s in streams:
            try:
                s.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass

    def _drop_stream(self, stream: Any) -> None:
        """Remove *stream* from the active list and close it.

        Called from the PortAudio ``finished_callback`` (audio thread)
        and from :meth:`close` (user thread). The lock keeps the two
        paths from racing on ``self._active_streams``.
        """
        with self._streams_lock:
            try:
                self._active_streams.remove(stream)
            except ValueError:
                pass
        try:
            stream.close()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass


def create_default_player(sample_rate: int = 44100) -> ChirpPlayer:
    """Return the best available :class:`ChirpPlayer` for this environment.

    Returns a :class:`SoundDeviceChirpPlayer` when ``sounddevice`` is
    importable, else a :class:`SilentChirpPlayer`. Import errors are
    logged at WARNING — never raised — so the SDK stays usable on
    headless machines with no audio output, but interactive users see
    the explicit "install ``syncfield[audio]`` to hear chirps" hint
    instead of silently wondering why nothing beeps.
    """
    try:
        import sounddevice  # noqa: F401
    except (ImportError, OSError) as exc:
        logger.warning(
            "sounddevice failed to load (%s). The 3/2/1 countdown and "
            "start/stop chirps will be SILENT. sounddevice ships with "
            "syncfield by default; on Linux you may need the system "
            "PortAudio package: `apt install libportaudio2`.",
            exc,
        )
        return SilentChirpPlayer()
    return SoundDeviceChirpPlayer(sample_rate=sample_rate)
