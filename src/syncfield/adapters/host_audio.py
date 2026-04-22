"""HostAudioStream — records host microphone audio for cross-host sync.

Captures audio from the system's default input device (or a specified
device) and writes a WAV file to the episode directory. The recorded
audio contains the chirp signals that the SyncField Docker container
uses for cross-correlation alignment between hosts.

Also emits real-time RMS and peak level metrics as ``SampleEvent``
channels so the viewer can display a live audio level indicator.

Usage::

    from syncfield.adapters import HostAudioStream

    session.add(HostAudioStream("host_audio", output_dir=session.output_dir))

In most cases you don't add this manually — the orchestrator auto-injects
it when no other stream provides an audio track. See
:meth:`SessionOrchestrator.connect` for the auto-inject logic.

Requires the ``audio`` extra::

    pip install 'syncfield[audio]'
"""

from __future__ import annotations

import logging
import math
import time
import wave
from pathlib import Path
from typing import Any, Optional

from syncfield.clock import SessionClock
from syncfield.stream import StreamBase
from syncfield.types import (
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    StreamCapabilities,
)

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 44100
DEFAULT_CHANNELS = 1
METRICS_INTERVAL_S = 0.1  # Emit RMS/peak every ~100ms


# ---------------------------------------------------------------------------
# Audio device detection
# ---------------------------------------------------------------------------


def is_audio_available() -> bool:
    """Return True if sounddevice is importable and a default input exists."""
    try:
        device = get_default_input_device()
        return device is not None
    except Exception:
        return False


def get_default_input_device() -> Optional[dict]:
    """Return sounddevice device info for the default input, or None."""
    try:
        import sounddevice as sd
        device_info = sd.query_devices(kind="input")
        if device_info and device_info.get("max_input_channels", 0) > 0:
            return device_info
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HostAudioStream
# ---------------------------------------------------------------------------


class HostAudioStream(StreamBase):
    """Records host microphone audio to WAV for cross-host sync.

    The audio track captures chirp signals played by the orchestrator.
    These chirps serve as acoustic anchors for multi-host alignment
    via cross-correlation in the SyncField Docker container.

    Lifecycle:
    - ``connect()`` — open a preview input stream (metrics only, no file).
    - ``start_recording()`` — start writing WAV + emitting recorded samples.
    - ``stop_recording()`` — close WAV, stop recording. Preview continues.
    - ``disconnect()`` — close the input stream entirely.

    Args:
        id: Stream identifier (e.g. ``"host_audio"``).
        output_dir: Episode directory where ``{id}.wav`` will be written.
        device: sounddevice device index or name. ``None`` uses the
            system default input.
        sample_rate: Sample rate in Hz. Default 44100.
        channels: Number of audio channels. Default 1 (mono).
    """

    def __init__(
        self,
        id: str,
        output_dir: Path | str,
        *,
        device: int | str | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
    ) -> None:
        super().__init__(
            id=id,
            kind="audio",
            capabilities=StreamCapabilities(
                provides_audio_track=True,
                supports_precise_timestamps=True,
                is_removable=True,
                produces_file=True,
                target_hz=1.0 / METRICS_INTERVAL_S,
            ),
        )
        self._output_dir = Path(output_dir)
        self._device = device
        self._sample_rate = sample_rate
        self._channels = channels

        self._stream: Any = None  # sounddevice.InputStream
        self._wav_writer: Optional[wave.Wave_write] = None
        self._wav_path: Optional[Path] = None
        self._recording = False
        self._connected = False

        self._frame_count = 0
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None

        # Real-time level metrics
        self._last_metrics_time = 0.0

    # ------------------------------------------------------------------
    # 4-phase lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the audio input stream for live preview (no file writing)."""
        import numpy as np
        import sounddevice as sd

        if self._device is not None:
            info = sd.query_devices(self._device, kind="input")
        else:
            info = sd.query_devices(kind="input")

        if not info or info.get("max_input_channels", 0) < self._channels:
            raise RuntimeError(
                f"Audio device has insufficient input channels "
                f"(need {self._channels}, have "
                f"{info.get('max_input_channels', 0) if info else 0})"
            )

        # Audio callback — runs on PortAudio thread
        def _audio_callback(indata, frames, time_info, status):
            if status:
                self._emit_health(HealthEvent(
                    self.id, HealthEventKind.WARNING,
                    time.monotonic_ns(), f"Audio status: {status}",
                ))

            capture_ns = time.monotonic_ns()
            mono = indata[:, 0]

            # Write to WAV if recording
            if self._recording and self._wav_writer is not None:
                pcm16 = (mono * 32767).astype(np.int16)
                self._wav_writer.writeframes(pcm16.tobytes())
                if self._first_at is None:
                    self._first_at = capture_ns
                self._last_at = capture_ns
                self._frame_count += frames

            # Always emit RMS/peak for viewer (preview + recording)
            self._emit_audio_metrics(mono, capture_ns)

        self._stream = sd.InputStream(
            samplerate=self._sample_rate,
            channels=self._channels,
            dtype="float32",
            device=self._device,
            callback=_audio_callback,
            blocksize=int(self._sample_rate * METRICS_INTERVAL_S),
        )
        self._stream.start()
        self._connected = True

        logger.info(
            "[%s] Audio input connected: %s (%d Hz, %d ch)",
            self.id, info.get("name", "unknown"),
            self._sample_rate, self._channels,
        )

    def start_recording(self, session_clock: SessionClock) -> None:
        """Start writing audio to WAV file."""
        self._frame_count = 0
        self._first_at = None
        self._last_at = None

        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._wav_path = self._output_dir / f"{self.id}.wav"
        self._wav_writer = wave.open(str(self._wav_path), "wb")
        self._wav_writer.setnchannels(self._channels)
        self._wav_writer.setsampwidth(2)  # 16-bit
        self._wav_writer.setframerate(self._sample_rate)

        self._recording = True
        logger.info("[%s] Recording started → %s", self.id, self._wav_path)

    def stop_recording(self) -> FinalizationReport:
        """Stop recording, close WAV file, return report."""
        self._recording = False

        if self._wav_writer is not None:
            try:
                self._wav_writer.close()
            except Exception:
                pass
            self._wav_writer = None

        logger.info(
            "[%s] Recording stopped. %d frames → %s",
            self.id, self._frame_count, self._wav_path,
        )

        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=self._frame_count,
            file_path=str(self._wav_path) if self._wav_path else None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=None,
        )

    def disconnect(self) -> None:
        """Close the audio input stream."""
        self._recording = False

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        self._connected = False

    # Legacy one-shot compatibility
    def prepare(self) -> None:
        pass

    def start(self, session_clock: SessionClock) -> None:  # type: ignore[override]
        self.connect()
        self.start_recording(session_clock)

    def stop(self) -> FinalizationReport:
        report = self.stop_recording()
        self.disconnect()
        return report

    # ------------------------------------------------------------------
    # Real-time audio metrics
    # ------------------------------------------------------------------

    def _emit_audio_metrics(self, samples, capture_ns: int) -> None:
        """Compute and emit RMS/peak from a block of float32 samples."""
        now = time.monotonic()
        if now - self._last_metrics_time < METRICS_INTERVAL_S:
            return
        self._last_metrics_time = now

        if len(samples) == 0:
            return

        sq_sum = sum(float(s) * float(s) for s in samples)
        rms = math.sqrt(sq_sum / len(samples))
        peak = float(max(abs(float(s)) for s in samples))

        self._emit_sample(SampleEvent(
            stream_id=self.id,
            frame_number=self._frame_count,
            capture_ns=capture_ns,
            channels={"rms": rms, "peak": peak},
        ))
