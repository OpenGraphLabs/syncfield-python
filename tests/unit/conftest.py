"""Shared fixtures for unit tests."""

from unittest.mock import patch

import pytest

from syncfield.testing import FakeStream as _BaseFakeStream
from syncfield.types import StreamKind


class FakeStream(_BaseFakeStream):
    """Test-only FakeStream that also accepts a ``kind`` kwarg.

    The public :class:`syncfield.testing.FakeStream` hard-codes
    ``kind="custom"`` because its original purpose was exercising
    generic Stream SPI behaviour. Multi-host tests need to assert that
    the orchestrator treats audio-capable streams differently, so this
    conftest-local subclass threads ``kind`` through to ``StreamBase``
    while preserving every other behaviour of the public helper.
    """

    def __init__(
        self,
        id: str,
        kind: StreamKind = "video",
        provides_audio_track: bool = False,
        fail_on_prepare: bool = False,
        fail_on_start: bool = False,
        fail_on_stop: bool = False,
    ) -> None:
        super().__init__(
            id=id,
            provides_audio_track=provides_audio_track,
            fail_on_prepare=fail_on_prepare,
            fail_on_start=fail_on_start,
            fail_on_stop=fail_on_stop,
        )
        # StreamBase stores ``kind`` as a public attribute; overwrite
        # the "custom" default set by the public FakeStream.
        self.kind = kind


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
