"""Shared fixtures for unit tests."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _disable_audio_auto_inject():
    """Disable host audio auto-injection in all unit tests.

    The orchestrator auto-detects host microphones and injects a
    HostAudioStream during add(). This interferes with tests that
    assert exact stream counts. Patch it out globally so tests run
    consistently on machines with and without audio hardware.

    Tests in test_orchestrator_auto_audio.py override this by
    patching explicitly within each test.
    """
    with patch(
        "syncfield.orchestrator.SessionOrchestrator._maybe_preregister_host_audio"
    ), patch(
        "syncfield.orchestrator.SessionOrchestrator._maybe_inject_host_audio"
    ):
        yield
