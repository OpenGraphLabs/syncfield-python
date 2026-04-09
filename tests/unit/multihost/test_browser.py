"""Tests for :class:`SessionBrowser` with a fake zeroconf backend."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from syncfield.multihost.browser import SessionBrowser
from syncfield.multihost.types import SessionAnnouncement


class _FakeServiceInfo:
    def __init__(self, properties: Dict[bytes, bytes]) -> None:
        self.properties = properties


class _FakeZeroconf:
    def __init__(self) -> None:
        self._registered: Dict[str, _FakeServiceInfo] = {}
        self.closed = False

    def get_service_info(
        self, type_: str, name: str
    ) -> Optional[_FakeServiceInfo]:
        return self._registered.get(name)

    def register(self, name: str, info: _FakeServiceInfo) -> None:
        self._registered[name] = info

    def close(self) -> None:
        self.closed = True


class _FakeServiceBrowser:
    def __init__(self, zc: _FakeZeroconf, type_: str, listener: Any) -> None:
        self.zc = zc
        self.type_ = type_
        self.listener = listener
        self.cancelled = False

    def fire_add(self, name: str) -> None:
        self.listener.add_service(self.zc, self.type_, name)

    def fire_update(self, name: str) -> None:
        self.listener.update_service(self.zc, self.type_, name)

    def fire_remove(self, name: str) -> None:
        self.listener.remove_service(self.zc, self.type_, name)

    def cancel(self) -> None:
        self.cancelled = True


@pytest.fixture
def fake_backend(monkeypatch):
    zc = _FakeZeroconf()
    browsers: List[_FakeServiceBrowser] = []

    def zc_factory():
        return zc

    def browser_factory(zc_arg: Any, type_: str, listener: Any) -> _FakeServiceBrowser:
        b = _FakeServiceBrowser(zc_arg, type_, listener)
        browsers.append(b)
        return b

    monkeypatch.setattr(
        "syncfield.multihost.browser._get_zeroconf_cls", lambda: zc_factory
    )
    monkeypatch.setattr(
        "syncfield.multihost.browser._get_service_browser_cls",
        lambda: browser_factory,
    )
    return zc, browsers


def _announcement(
    session_id: str, status: str, **extra: Any
) -> SessionAnnouncement:
    base: Dict[str, Any] = dict(
        host_id="mac_lead",
        sdk_version="0.2.0",
        chirp_enabled=True,
    )
    base.update(extra)
    return SessionAnnouncement(
        session_id=session_id, status=status, **base  # type: ignore[arg-type]
    )


def _register(zc: _FakeZeroconf, ann: SessionAnnouncement) -> str:
    name = f"{ann.session_id}._syncfield._tcp.local."
    zc.register(name, _FakeServiceInfo(ann.to_txt_record()))
    return name


class TestLifecycle:
    def test_rejects_double_start(self, fake_backend):
        browser = SessionBrowser()
        browser.start()
        with pytest.raises(RuntimeError, match="already started"):
            browser.start()

    def test_close_cancels_browser_and_closes_zeroconf(self, fake_backend):
        zc, browsers = fake_backend
        browser = SessionBrowser()
        browser.start()
        browser.close()
        assert browsers[0].cancelled is True
        assert zc.closed is True

    def test_second_close_is_noop(self, fake_backend):
        browser = SessionBrowser()
        browser.start()
        browser.close()
        browser.close()  # must not raise


class TestObservation:
    def test_add_service_appends_announcement(self, fake_backend):
        zc, browsers = fake_backend
        browser = SessionBrowser()
        browser.start()
        ann = _announcement("amber-tiger-042", "preparing")
        name = _register(zc, ann)
        browsers[0].fire_add(name)
        observed = browser.current_sessions()
        assert len(observed) == 1
        assert observed[0].session_id == "amber-tiger-042"
        assert observed[0].status == "preparing"

    def test_remove_service_drops_announcement(self, fake_backend):
        zc, browsers = fake_backend
        browser = SessionBrowser()
        browser.start()
        ann = _announcement("amber-tiger-042", "preparing")
        name = _register(zc, ann)
        browsers[0].fire_add(name)
        browsers[0].fire_remove(name)
        assert browser.current_sessions() == []


class TestWaitForRecording:
    def test_returns_when_status_updates(self, fake_backend):
        zc, browsers = fake_backend
        browser = SessionBrowser(session_id="amber-tiger-042")
        browser.start()

        def simulate():
            time.sleep(0.02)
            prep = _announcement("amber-tiger-042", "preparing")
            name = _register(zc, prep)
            browsers[0].fire_add(name)
            time.sleep(0.02)
            rec = _announcement(
                "amber-tiger-042", "recording", started_at_ns=9999
            )
            _register(zc, rec)
            browsers[0].fire_update(name)

        threading.Thread(target=simulate, daemon=True).start()
        observed = browser.wait_for_recording(timeout=1.0)
        assert observed.status == "recording"
        assert observed.started_at_ns == 9999

    def test_timeout_raises(self, fake_backend):
        browser = SessionBrowser(session_id="does-not-exist")
        browser.start()
        with pytest.raises(TimeoutError, match="recording"):
            browser.wait_for_recording(timeout=0.05)

    def test_filter_ignores_non_matching_session_id(self, fake_backend):
        zc, browsers = fake_backend
        browser = SessionBrowser(session_id="amber-tiger-042")
        browser.start()
        # Register a DIFFERENT session_id in recording state — must be
        # ignored and wait should time out.
        other = _announcement("other-session-001", "recording", started_at_ns=1)
        name = _register(zc, other)
        browsers[0].fire_add(name)
        with pytest.raises(TimeoutError):
            browser.wait_for_recording(timeout=0.05)

    def test_no_filter_accepts_any_leader(self, fake_backend):
        zc, browsers = fake_backend
        browser = SessionBrowser()  # no filter
        browser.start()
        ann = _announcement("any-session-001", "recording", started_at_ns=1)
        name = _register(zc, ann)
        browsers[0].fire_add(name)
        observed = browser.wait_for_recording(timeout=0.1)
        assert observed.session_id == "any-session-001"


class TestWaitForStopped:
    def test_returns_when_status_transitions_to_stopped(self, fake_backend):
        zc, browsers = fake_backend
        browser = SessionBrowser(session_id="amber-tiger-042")
        browser.start()

        def simulate():
            time.sleep(0.02)
            rec = _announcement(
                "amber-tiger-042", "recording", started_at_ns=1
            )
            name = _register(zc, rec)
            browsers[0].fire_add(name)
            time.sleep(0.02)
            stp = _announcement(
                "amber-tiger-042", "stopped", started_at_ns=1
            )
            _register(zc, stp)
            browsers[0].fire_update(name)

        threading.Thread(target=simulate, daemon=True).start()
        observed = browser.wait_for_stopped(timeout=1.0)
        assert observed.status == "stopped"
