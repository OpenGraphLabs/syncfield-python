"""Tests for the orchestrator's auto audio injection logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from syncfield.orchestrator import SessionOrchestrator
from syncfield.testing import FakeStream
from syncfield.tone import SyncToneConfig


@pytest.fixture(autouse=True)
def _disable_audio_auto_inject():
    """Override conftest.py — this module *tests* auto-audio injection.

    The package-level conftest patches both ``_maybe_preregister_host_audio``
    and ``_maybe_inject_host_audio`` away so other unit tests can assert
    exact stream counts without a live mic interfering. Tests in this
    file need the real injection path to run; per-test patches handle
    everything else (mic discovery, sounddevice mocking).
    """
    yield


def _session(tmp_path: Path, **kwargs) -> SessionOrchestrator:
    return SessionOrchestrator(
        host_id=kwargs.pop("host_id", "rig_01"),
        output_dir=tmp_path,
        sync_tone=kwargs.pop("sync_tone", SyncToneConfig.default()),
        **kwargs,
    )


class TestAutoAudioInjection:
    def test_injects_when_no_audio_stream(self, tmp_path: Path):
        """Should auto-inject HostAudioStream when no audio track exists."""
        session = _session(tmp_path)

        mock_info = {"name": "Test Mic", "max_input_channels": 1, "default_high_input_latency": 0.01}
        with patch("syncfield.adapters.host_audio.is_audio_available", return_value=True), \
             patch("syncfield.orchestrator.SessionOrchestrator._maybe_preregister_host_audio"), \
             patch("syncfield.adapters.host_audio.is_audio_available", return_value=True), \
             patch("sounddevice.query_devices", return_value=mock_info), \
             patch("sounddevice.InputStream"):
            session.add(FakeStream("cam"))
            session.connect()

        assert "host_audio" in session._streams
        assert session._auto_audio_stream is not None
        assert session._streams["host_audio"].capabilities.provides_audio_track is True

    def test_skips_when_audio_stream_exists(self, tmp_path: Path):
        """Should NOT inject when user already added an audio-capable stream."""
        session = _session(tmp_path)
        audio_stream = FakeStream(
            "user_mic",
            provides_audio_track=True,
        )
        session.add(audio_stream)

        session.connect()

        assert "host_audio" not in session._streams
        assert session._auto_audio_stream is None

    def test_skips_when_no_sounddevice(self, tmp_path: Path):
        """Should gracefully skip when audio extra is not installed."""
        session = _session(tmp_path)

        # Patch before add() so pre-registration is also blocked
        with patch.dict("sys.modules", {"syncfield.adapters.host_audio": None}):
            session.add(FakeStream("cam"))
            session.connect()

        assert "host_audio" not in session._streams
        assert session._auto_audio_stream is None

    def test_skips_when_no_mic_detected(self, tmp_path: Path):
        """Should skip when is_audio_available returns False."""
        session = _session(tmp_path)

        with patch("syncfield.adapters.host_audio.is_audio_available", return_value=False):
            session.add(FakeStream("cam"))
            session.connect()

        assert "host_audio" not in session._streams

    def test_skips_when_enable_host_audio_false(self, tmp_path: Path):
        """Should NOT inject when enable_host_audio=False, even if mic is available."""
        session = _session(tmp_path, enable_host_audio=False)

        mock_info = {"name": "Test Mic", "max_input_channels": 1, "default_high_input_latency": 0.01}
        with patch("syncfield.adapters.host_audio.is_audio_available", return_value=True), \
             patch("sounddevice.query_devices", return_value=mock_info), \
             patch("sounddevice.InputStream"):
            session.add(FakeStream("cam"))
            session.connect()

        assert "host_audio" not in session._streams
        assert session._auto_audio_stream is None

    def test_kept_on_disconnect(self, tmp_path: Path):
        """Auto-injected stream should stay registered (visible) after disconnect."""
        session = _session(tmp_path)

        mock_info = {"name": "Test Mic", "max_input_channels": 1, "default_high_input_latency": 0.01}
        with patch("syncfield.adapters.host_audio.is_audio_available", return_value=True), \
             patch("syncfield.orchestrator.SessionOrchestrator._maybe_preregister_host_audio"), \
             patch("sounddevice.query_devices", return_value=mock_info), \
             patch("sounddevice.InputStream"):
            session.add(FakeStream("cam"))
            session.connect()

        assert "host_audio" in session._streams

        session.disconnect()

        # Stream stays registered so it remains visible in the viewer
        assert "host_audio" in session._streams
        assert session._auto_audio_stream is not None
