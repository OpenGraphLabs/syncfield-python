"""End-to-end multi-host rendezvous over real Zeroconf on loopback.

Marked ``slow`` so the default unit-test run stays snappy; invoke with
``pytest -m slow`` to include.

These tests instantiate the real :class:`SessionAdvertiser` and
:class:`SessionBrowser` without any mocks and drive them through the
full preparing → recording → stopped status transitions. If zeroconf
cannot bind to the local interface (CI without mDNS, sandboxed
environments, no active network, …) the whole module is skipped.

The comprehensive logic coverage lives in the unit-test suite under
``tests/unit/multihost/`` (38 tests with a fake zeroconf backend).
These integration tests are a thin smoke check that the real wire
format round-trips — they intentionally have fewer assertions and
rely on OS mDNS infrastructure.
"""

from __future__ import annotations

import threading
import time

import pytest

zeroconf_mod = pytest.importorskip("zeroconf")

from syncfield.multihost.advertiser import SessionAdvertiser  # noqa: E402
from syncfield.multihost.browser import SessionBrowser  # noqa: E402

pytestmark = pytest.mark.slow


def _probe_mdns_available() -> bool:
    """Return ``True`` when the host has a working mDNS multicast stack.

    Zeroconf binds to ``224.0.0.251:5353`` on every available
    interface. On macOS without an active network, on sandboxed CI
    runners, or inside containers without host networking, that bind
    fails with ``OSError(49, "Can't assign requested address")`` —
    but the failure happens asynchronously on zeroconf's engine
    thread and is only logged as a warning. Merely constructing a
    ``Zeroconf`` instance therefore is not enough to confirm the
    stack is usable.

    This probe actually tries to register a dummy service and browse
    for it with a short 1.5 s deadline. If the round trip completes,
    the host has a working mDNS path; if it times out, we skip.
    """
    import socket

    try:
        zc = zeroconf_mod.Zeroconf()
    except Exception:
        return False
    try:
        info = zeroconf_mod.ServiceInfo(
            "_syncfieldprobe._tcp.local.",
            "probe._syncfieldprobe._tcp.local.",
            port=0,
            properties={b"probe": b"1"},
            server=f"{socket.gethostname()}.local.",
        )
        try:
            zc.register_service(info)
        except Exception:
            return False

        got_event = threading.Event()
        resolved_properties: list = []

        class _ProbeListener:
            def add_service(self, zc, type_, name):
                # Resolve the TXT record synchronously to verify the
                # full mDNS path (not just the listener callback)
                # is working. This is the SAME pattern the
                # SessionBrowser uses, so if the probe succeeds the
                # real browser is guaranteed to work too.
                try:
                    info = zc.get_service_info(
                        "_syncfieldprobe._tcp.local.", name, timeout=1500
                    )
                except Exception:
                    return
                if info is not None and getattr(info, "properties", None):
                    resolved_properties.append(dict(info.properties))
                    got_event.set()

            def update_service(self, zc, type_, name):
                self.add_service(zc, type_, name)

            def remove_service(self, zc, type_, name):
                pass

        browser = zeroconf_mod.ServiceBrowser(
            zc, "_syncfieldprobe._tcp.local.", listener=_ProbeListener()
        )
        try:
            available = got_event.wait(timeout=3.0)
        finally:
            try:
                browser.cancel()
            except Exception:
                pass
            try:
                zc.unregister_service(info)
            except Exception:
                pass
        return available
    finally:
        try:
            zc.close()
        except Exception:
            pass


_MDNS_AVAILABLE = _probe_mdns_available()
_mdns_required = pytest.mark.skipif(
    not _MDNS_AVAILABLE,
    reason="no working mDNS multicast socket on this host",
)


@_mdns_required
def test_leader_advertises_then_follower_observes_recording():
    """A leader's update_status('recording') must reach a live browser."""
    advertiser = SessionAdvertiser(
        session_id="integration-test-001",
        host_id="leader",
        sdk_version="0.2.0",
        chirp_enabled=True,
        graceful_shutdown_ms=200,
    )
    browser = SessionBrowser(session_id="integration-test-001")

    advertiser.start()
    browser.start()
    try:
        def promote():
            time.sleep(0.3)
            advertiser.update_status("recording", started_at_ns=42)

        t = threading.Thread(target=promote, daemon=True)
        t.start()

        observed = browser.wait_for_recording(timeout=5.0)
        t.join(timeout=1.0)

        assert observed.session_id == "integration-test-001"
        assert observed.status == "recording"
        assert observed.host_id == "leader"
        assert observed.started_at_ns == 42
    finally:
        browser.close()
        advertiser.close()


@_mdns_required
def test_leader_stopped_transition_reaches_follower():
    """Follower's wait_for_stopped must fire when leader flips to stopped."""
    advertiser = SessionAdvertiser(
        session_id="integration-test-002",
        host_id="leader",
        sdk_version="0.2.0",
        chirp_enabled=True,
        graceful_shutdown_ms=100,
    )
    browser = SessionBrowser(session_id="integration-test-002")

    advertiser.start()
    advertiser.update_status("recording", started_at_ns=1)
    browser.start()
    try:
        def flip_stopped():
            time.sleep(0.3)
            advertiser.update_status("stopped")

        t = threading.Thread(target=flip_stopped, daemon=True)
        t.start()

        observed = browser.wait_for_stopped(timeout=5.0)
        t.join(timeout=1.0)

        assert observed.status == "stopped"
        assert observed.session_id == "integration-test-002"
    finally:
        browser.close()
        advertiser.close()
