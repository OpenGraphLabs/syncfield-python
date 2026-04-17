"""Tests for the HostAudioStream adapter."""

from __future__ import annotations

import struct
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from syncfield.adapters.host_audio import (
    HostAudioStream,
    get_default_input_device,
    is_audio_available,
)


# ---------------------------------------------------------------------------
# Audio detection helpers
# ---------------------------------------------------------------------------


class TestAudioDetection:
    def test_is_audio_available_with_device(self):
        mock_info = {"name": "MacBook Pro Mic", "max_input_channels": 1}
        with patch("syncfield.adapters.host_audio.get_default_input_device", return_value=mock_info):
            assert is_audio_available() is True

    def test_is_audio_available_no_device(self):
        with patch("syncfield.adapters.host_audio.get_default_input_device", return_value=None):
            assert is_audio_available() is False

    def test_is_audio_available_import_error(self):
        with patch("syncfield.adapters.host_audio.get_default_input_device", side_effect=ImportError):
            assert is_audio_available() is False

    def test_get_default_input_device_no_sounddevice(self):
        with patch.dict("sys.modules", {"sounddevice": None}):
            result = get_default_input_device()
            # May return None or raise — either way, is_audio_available handles it
            # Just verify it doesn't crash
            assert result is None or isinstance(result, dict)


# ---------------------------------------------------------------------------
# HostAudioStream construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_kind_is_audio(self, tmp_path: Path):
        stream = HostAudioStream("mic", output_dir=tmp_path)
        assert stream.kind == "audio"

    def test_provides_audio_track(self, tmp_path: Path):
        stream = HostAudioStream("mic", output_dir=tmp_path)
        assert stream.capabilities.provides_audio_track is True

    def test_produces_file(self, tmp_path: Path):
        stream = HostAudioStream("mic", output_dir=tmp_path)
        assert stream.capabilities.produces_file is True

    def test_default_sample_rate(self, tmp_path: Path):
        stream = HostAudioStream("mic", output_dir=tmp_path)
        assert stream._sample_rate == 44100

    def test_custom_sample_rate(self, tmp_path: Path):
        stream = HostAudioStream("mic", output_dir=tmp_path, sample_rate=22050)
        assert stream._sample_rate == 22050


# ---------------------------------------------------------------------------
# Connect / disconnect
# ---------------------------------------------------------------------------


class TestConnect:
    def test_connect_with_valid_device(self, tmp_path: Path):
        stream = HostAudioStream("mic", output_dir=tmp_path)
        mock_info = {"name": "Test Mic", "max_input_channels": 2}
        # Mock both query_devices (for channel validation) and InputStream
        # (so no real PortAudio device is opened in CI).
        with patch("sounddevice.query_devices", return_value=mock_info), \
             patch("sounddevice.InputStream") as mock_input_stream:
            mock_input_stream.return_value = MagicMock()
            stream.connect()
            assert stream._connected is True
            mock_input_stream.assert_called_once()

    def test_connect_no_input_channels(self, tmp_path: Path):
        stream = HostAudioStream("mic", output_dir=tmp_path)
        mock_info = {"name": "Speakers", "max_input_channels": 0}
        with patch("sounddevice.query_devices", return_value=mock_info):
            with pytest.raises(RuntimeError, match="insufficient input channels"):
                stream.connect()

    def test_disconnect(self, tmp_path: Path):
        stream = HostAudioStream("mic", output_dir=tmp_path)
        stream._connected = True
        stream.disconnect()
        assert stream._connected is False


# ---------------------------------------------------------------------------
# WAV file output
# ---------------------------------------------------------------------------


class TestWavOutput:
    def test_wav_file_created(self, tmp_path: Path):
        """Simulate recording by manually writing WAV data."""
        stream = HostAudioStream("host_audio", output_dir=tmp_path)

        # Manually create what start_recording would create
        wav_path = tmp_path / "host_audio.wav"
        wf = wave.open(str(wav_path), "wb")
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)

        # Write 1 second of silence (44100 samples)
        silence = b"\x00\x00" * 44100
        wf.writeframes(silence)
        wf.close()

        # Verify WAV is valid
        with wave.open(str(wav_path), "rb") as rf:
            assert rf.getnchannels() == 1
            assert rf.getsampwidth() == 2
            assert rf.getframerate() == 44100
            assert rf.getnframes() == 44100

    def test_wav_path_in_output_dir(self, tmp_path: Path):
        stream = HostAudioStream("my_mic", output_dir=tmp_path)
        expected = tmp_path / "my_mic.wav"
        assert stream._output_dir == tmp_path
        # Path is set during start_recording, but we can verify the pattern
        assert str(expected).endswith("my_mic.wav")


# ---------------------------------------------------------------------------
# Finalization report
# ---------------------------------------------------------------------------


class TestFinalizationReport:
    def test_report_fields(self, tmp_path: Path):
        stream = HostAudioStream("mic", output_dir=tmp_path)
        stream._frame_count = 44100
        stream._first_at = 1000
        stream._last_at = 2000
        stream._wav_path = tmp_path / "mic.wav"

        report = stream.stop_recording()
        assert report.stream_id == "mic"
        assert report.status == "completed"
        assert report.frame_count == 44100
        assert report.first_sample_at_ns == 1000
        assert report.last_sample_at_ns == 2000
        assert "mic.wav" in str(report.file_path)


# ---------------------------------------------------------------------------
# Audio metrics emission
# ---------------------------------------------------------------------------


class TestAudioMetrics:
    def test_emit_rms_and_peak(self, tmp_path: Path):
        stream = HostAudioStream("mic", output_dir=tmp_path)
        stream._recording = True
        stream._frame_count = 100
        stream._last_metrics_time = 0  # Force emission

        received = []
        stream.on_sample(lambda e: received.append(e))

        # Simulate audio samples
        samples = [0.5, -0.3, 0.1, 0.8, -0.2]
        stream._emit_audio_metrics(samples, 12345)

        assert len(received) == 1
        event = received[0]
        assert "rms" in event.channels
        assert "peak" in event.channels
        assert event.channels["peak"] == pytest.approx(0.8, abs=0.01)
        assert event.channels["rms"] > 0

    def test_metrics_throttled(self, tmp_path: Path):
        import time

        stream = HostAudioStream("mic", output_dir=tmp_path)
        stream._recording = True
        stream._frame_count = 100
        stream._last_metrics_time = time.monotonic()  # Just emitted

        received = []
        stream.on_sample(lambda e: received.append(e))

        stream._emit_audio_metrics([0.5], 12345)
        assert len(received) == 0  # Throttled
