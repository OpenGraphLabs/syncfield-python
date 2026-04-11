# Host Audio Auto-Capture Design Spec

**Date:** 2026-04-11
**Status:** Approved

## Summary

Automatically record host microphone audio during sessions for multi-host cross-correlation sync. The orchestrator auto-detects audio input and injects a `HostAudioStream` when no other stream provides an audio track. WAV waveform visualization in both Recording and Review modes.

## Decisions

| Item | Decision |
|------|----------|
| Approach | Hybrid — standalone adapter + orchestrator auto-inject |
| File format | WAV (44.1 kHz, 16-bit, mono) via stdlib `wave` module |
| Dependencies | `sounddevice` + `numpy` (already in `audio` extra) |
| Auto-inject trigger | `orchestrator.connect()` — if no `provides_audio_track=True` stream exists |
| Graceful degradation | `audio` extra not installed or no mic → WARNING log, skip |
| Viz (Recording) | Real-time RMS/peak level via SSE (same path as sensor streams) |
| Viz (Review) | Downsampled waveform from WAV file, SVG rendering |

## Components

### 1. `HostAudioStream` adapter (`adapters/host_audio.py`)

StreamBase subclass, 4-phase lifecycle:

```python
class HostAudioStream(StreamBase):
    kind = "audio"
    capabilities = StreamCapabilities(
        provides_audio_track=True,
        supports_precise_timestamps=True,
        is_removable=True,
        produces_file=True,
    )
```

**Constructor:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `id` | `str` | required | Stream identifier |
| `output_dir` | `Path` | required | Episode directory for WAV output |
| `device` | `int \| str \| None` | `None` | sounddevice device index/name. `None` = system default |
| `sample_rate` | `int` | `44100` | Sample rate in Hz |
| `channels` | `int` | `1` | Number of audio channels (mono) |

**Lifecycle:**

- `connect()` — Validate device exists via `sounddevice.query_devices()`. Store device info. Don't open stream yet.
- `start_recording(session_clock)` — Open `sounddevice.InputStream`, open WAV file writer, start callback-based recording. Emit `SampleEvent` with `rms` and `peak` channels on each audio buffer (~10 Hz).
- `stop_recording()` — Close input stream, flush and close WAV file. Return `FinalizationReport` with frame count and file path.
- `disconnect()` — Release device reference.

**WAV Writing:**

- File path: `{output_dir}/{id}.wav`
- stdlib `wave` module — open on `start_recording`, write PCM16 frames in audio callback, close on `stop_recording`
- Audio callback runs on sounddevice's PortAudio thread — write directly, no queue needed (WAV write is fast enough)

**Real-time Metrics (for viewer):**

- Every ~100ms (configurable), compute RMS and peak from the latest audio buffer
- Emit as `SampleEvent(channels={"rms": float, "peak": float})`
- This flows through the existing poller → SSE → sensor chart pipeline

### 2. Audio Detection (`adapters/host_audio.py`)

Module-level functions:

```python
def is_audio_available() -> bool:
    """True if sounddevice is importable and a default input device exists."""

def get_default_input_device() -> dict | None:
    """Return sounddevice device info dict for the default input, or None."""
```

### 3. Orchestrator Auto-Inject (`orchestrator.py`)

In `connect()`, after all explicit streams are connected:

```python
# Auto-inject host audio if no stream provides an audio track
if not any(s.capabilities.provides_audio_track for s in self._streams.values()):
    try:
        from syncfield.adapters.host_audio import HostAudioStream, is_audio_available
        if is_audio_available():
            audio_stream = HostAudioStream("host_audio", output_dir=self._output_dir)
            self.add(audio_stream)
            audio_stream.connect()
            self._auto_audio_stream = audio_stream
            logger.info("Auto-injected host audio stream (mic detected)")
    except ImportError:
        logger.info("Audio extra not installed — skipping host audio capture")
```

- `_auto_audio_stream` field tracks the auto-injected stream
- On `disconnect()`, auto-remove the injected stream
- If user explicitly adds a `provides_audio_track=True` stream, auto-inject is skipped

### 4. Viewer — Recording Mode Visualization

**Backend (server.py):**
- No changes needed — `HostAudioStream` emits `SampleEvent` like any sensor
- The poller picks up `rms`/`peak` channels in `StreamStatsBuffer.observe_sample()`
- SSE endpoint `/stream/sensor/host_audio` pushes data

**Frontend:**
- The existing `StreamCard` with `kind="audio"` renders a new `AudioLevelChart` component instead of `SensorChart`
- `AudioLevelChart` — horizontal VU-meter style bar showing real-time RMS level with peak indicator
- Green → yellow → red gradient based on level

### 5. Viewer — Review Mode Visualization

**Backend (server.py):**

New endpoint: `GET /api/episodes/{id}/waveform/{stream_id}`

- Read the WAV file from the episode directory
- Downsample to ~1000 points (min/max envelope per bucket)
- Return JSON: `{"sample_rate": 44100, "duration_s": 15.2, "envelope": [[min, max], ...]}`
- Cached in memory after first read

**Frontend:**

New component: `WaveformChart` (used in episode detail)

- Renders downsampled waveform as SVG with min/max envelope fill
- Time axis aligned with the video timeline
- Chirp regions highlighted (using `sync_point.json` chirp_start_ns / chirp_stop_ns)
- Placed in the sidebar or below the drift chart

### 6. Episode Directory Output

```
episode_dir/
  host_audio.wav                    ← NEW: raw PCM16 mono WAV
  host_audio.timestamps.jsonl       ← sample writer (rms/peak per ~100ms)
  sync_point.json                   ← chirp_start_ns / chirp_stop_ns (existing)
  manifest.json                     ← includes host_audio stream entry
  {other streams}...
```

### 7. Tests

**`test_host_audio.py`:**
- WAV file creation and validity (can be read back by `wave` module)
- Lifecycle: connect → start_recording → stop_recording → disconnect
- RMS/peak channel emission during recording
- Graceful handling when no mic available (mock sounddevice)
- `is_audio_available()` with/without sounddevice

**`test_orchestrator_auto_audio.py`:**
- Auto-inject when no audio stream present
- Skip when user already added `provides_audio_track=True` stream
- Skip when `audio` extra not installed
- Auto-remove on disconnect
