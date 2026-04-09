"""Tests for :class:`SessionAdvertiser` with a fake zeroconf backend."""

from __future__ import annotations

from typing import Any, List

import pytest

from syncfield.multihost.advertiser import (
    SERVICE_TYPE,
    SessionAdvertiser,
)


class _FakeZeroconf:
    def __init__(self) -> None:
        self.registered: List[Any] = []
        self.updated: List[Any] = []
        self.unregistered: List[Any] = []
        self.closed = False

    def register_service(self, info: Any, **_: Any) -> None:
        self.registered.append(info)

    def update_service(self, info: Any) -> None:
        self.updated.append(info)

    def unregister_service(self, info: Any) -> None:
        self.unregistered.append(info)

    def close(self) -> None:
        self.closed = True


class _FakeServiceInfo:
    def __init__(self, type_: str, name: str, **kwargs: Any) -> None:
        self.type_ = type_
        self.name = name
        self.port = kwargs.get("port", 0)
        self.properties = kwargs.get("properties", {})
        self.server = kwargs.get("server", "")


@pytest.fixture
def fake_backend(monkeypatch):
    """Wire ``SessionAdvertiser`` to a synchronous fake backend."""
    zc = _FakeZeroconf()
    monkeypatch.setattr(
        "syncfield.multihost.advertiser._get_zeroconf_cls",
        lambda: (lambda: zc),
    )
    monkeypatch.setattr(
        "syncfield.multihost.advertiser._get_service_info_cls",
        lambda: _FakeServiceInfo,
    )
    return zc


def _make_advertiser(**overrides: Any) -> SessionAdvertiser:
    kwargs = dict(
        session_id="amber-tiger-042",
        host_id="mac_lead",
        sdk_version="0.2.0",
        chirp_enabled=True,
        graceful_shutdown_ms=0,  # don't sleep during tests
    )
    kwargs.update(overrides)
    return SessionAdvertiser(**kwargs)  # type: ignore[arg-type]


class TestConstruction:
    def test_rejects_invalid_session_id(self, fake_backend):
        with pytest.raises(ValueError, match="session_id"):
            _make_advertiser(session_id="with space")

    def test_initial_announcement_is_preparing(self, fake_backend):
        ad = _make_advertiser()
        assert ad.announcement.status == "preparing"
        assert ad.session_id == "amber-tiger-042"


class TestStart:
    def test_registers_service_with_preparing_status(self, fake_backend):
        ad = _make_advertiser()
        ad.start()
        assert len(fake_backend.registered) == 1
        info = fake_backend.registered[0]
        assert info.type_ == SERVICE_TYPE
        assert info.name == f"amber-tiger-042.{SERVICE_TYPE}"
        assert info.properties[b"status"] == b"preparing"
        assert info.properties[b"session_id"] == b"amber-tiger-042"
        assert info.properties[b"host_id"] == b"mac_lead"
        assert info.properties[b"chirp_enabled"] == b"1"

    def test_rejects_double_start(self, fake_backend):
        ad = _make_advertiser()
        ad.start()
        with pytest.raises(RuntimeError, match="already started"):
            ad.start()


class TestUpdateStatus:
    def test_to_recording_writes_started_at(self, fake_backend):
        ad = _make_advertiser()
        ad.start()
        ad.update_status("recording", started_at_ns=99)
        assert len(fake_backend.updated) == 1
        info = fake_backend.updated[0]
        assert info.properties[b"status"] == b"recording"
        assert info.properties[b"started_at_ns"] == b"99"

    def test_to_stopped_preserves_started_at(self, fake_backend):
        ad = _make_advertiser()
        ad.start()
        ad.update_status("recording", started_at_ns=99)
        ad.update_status("stopped")  # no started_at → must not erase
        info = fake_backend.updated[-1]
        assert info.properties[b"status"] == b"stopped"
        assert info.properties[b"started_at_ns"] == b"99"

    def test_before_start_raises(self, fake_backend):
        ad = _make_advertiser()
        with pytest.raises(RuntimeError, match="not started"):
            ad.update_status("recording")


class TestClose:
    def test_unregisters_and_closes_zeroconf(self, fake_backend):
        ad = _make_advertiser()
        ad.start()
        ad.close()
        assert len(fake_backend.unregistered) == 1
        assert fake_backend.closed is True

    def test_second_close_is_noop(self, fake_backend):
        ad = _make_advertiser()
        ad.start()
        ad.close()
        ad.close()  # must not raise
        assert len(fake_backend.unregistered) == 1  # still only one
