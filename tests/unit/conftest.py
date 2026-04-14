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


class _InertAdvertiser:
    """Dummy stand-in for SessionAdvertiser used by the unit-test conftest.

    Implements every method the orchestrator calls so swapping it in
    for the real class is transparent to tests that don't care about
    advertiser behaviour. Tests that DO assert on advertiser activity
    must monkey-patch ``syncfield.orchestrator.SessionAdvertiser``
    themselves — that local patch overrides this conftest default.

    Critical: never touches zeroconf, so no mDNS sockets, no service
    registration, no NonUniqueNameException under xdist.
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass

    def update_status(self, *args, **kwargs):
        pass

    def close(self):
        pass


class _InertBrowser:
    """Dummy stand-in for SessionBrowser. See :class:`_InertAdvertiser`."""

    def __init__(self, session_id=None):
        self.session_id_filter = session_id

    def start(self):
        pass

    def close(self):
        pass

    def current_sessions(self):
        return []

    def wait_for_observation(self, timeout=30.0):
        return None

    def wait_for_recording(self, timeout=30.0):
        return None

    def wait_for_stopped(self, timeout=30.0):
        return None


@pytest.fixture(autouse=True)
def _disable_multihost_network(request):
    """Replace mDNS classes with inert dummies so unit tests never hit zeroconf.

    Since refactor 64dd0fd, ``SessionOrchestrator(role=LeaderRole|FollowerRole, ...)``
    instantiates ``SessionAdvertiser`` (which calls ``zeroconf.register_service``
    on ``.start()``) and ``SessionBrowser`` (which spawns a ``ServiceBrowser``)
    inside ``__init__``. Without this fixture, every unit test that uses a
    role would block on real mDNS — slow on macOS, flaky on shared machines,
    and a source of NonUniqueNameException collisions under ``pytest-xdist``.

    The control plane is NOT patched: it binds ``port=0`` (OS-assigned),
    starts uvicorn in a background thread, and is fast enough that tests
    asserting on ``session._control_plane is not None`` keep working.

    Tests that genuinely need the real ``SessionAdvertiser`` /
    ``SessionBrowser`` (e.g. multi-host integration smoke tests) must
    add ``@pytest.mark.real_multihost`` to opt out of this fixture.
    """
    if request.node.get_closest_marker("real_multihost"):
        yield
        return
    with patch(
        "syncfield.orchestrator.SessionAdvertiser", _InertAdvertiser
    ), patch(
        "syncfield.orchestrator.SessionBrowser", _InertBrowser
    ):
        yield
