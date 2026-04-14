"""mDNS service browsing for SyncField session followers.

A :class:`SessionBrowser` opens a single ``ServiceBrowser`` subscribed
to the ``_syncfield._tcp.local.`` service type and exposes two blocking
helpers — :meth:`wait_for_recording` and :meth:`wait_for_stopped` —
that followers use to keep their lifecycle in step with the leader.

Designed to be used inside
:meth:`syncfield.orchestrator.SessionOrchestrator.start` on the
follower: construct → :meth:`start` → :meth:`wait_for_recording` →
orchestrator starts its streams → (during session) →
:meth:`wait_for_stopped` → orchestrator.stop() → :meth:`close`.

The ``zeroconf`` import is lazy (see ``_get_zeroconf_cls``) so the
module stays importable on hosts that haven't installed the
``multihost`` extra — import side effects never touch the network.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from syncfield.multihost.advertiser import SERVICE_TYPE
from syncfield.multihost.types import SessionAnnouncement

logger = logging.getLogger(__name__)


def _get_zeroconf_cls() -> Callable[[], Any]:
    """Return a zero-argument factory for a ``Zeroconf`` instance.

    The returned factory binds ``interfaces=InterfaceChoice.All`` so the
    Zeroconf instance listens on every interface — including loopback,
    which the default ``InterfaceChoice.Default`` often excludes on
    macOS. Without this, two SyncField processes on the same MacBook
    cannot discover each other via mDNS.
    """
    from zeroconf import InterfaceChoice, Zeroconf  # type: ignore[import-not-found]

    return lambda: Zeroconf(interfaces=InterfaceChoice.All)


def _get_service_browser_cls() -> Callable[..., Any]:
    """Return a factory for ``ServiceBrowser``. See :func:`_get_zeroconf_cls`."""
    from zeroconf import ServiceBrowser  # type: ignore[import-not-found]

    return ServiceBrowser


class SessionBrowser:
    """Observes SyncField session advertisements on the local network.

    The browser keeps an in-memory dict of announcements keyed by the
    mDNS service name. Both the ``ServiceListener`` callbacks (invoked
    on the zeroconf thread) and the public wait methods (invoked on
    the user thread) touch that dict under a single
    :class:`~threading.Condition` so wait methods can block on
    status transitions without polling.

    Args:
        session_id: Optional filter. When set, only announcements
            whose session id matches are eligible to satisfy a
            ``wait_for_*`` call. When ``None``, the browser accepts
            any leader and picks the first one to reach the target
            status — suitable for single-leader environments where
            the operator doesn't want to enter a session id by hand.
    """

    #: Seconds between periodic re-resolutions of every known session.
    #: python-zeroconf's ``update_service`` listener callback is
    #: unreliable on macOS (mDNSResponder contends with python-zeroconf
    #: for unicast SRV/TXT replies), so we can't depend on it firing
    #: when the leader flips its advertised status from ``"preparing"``
    #: to ``"recording"``. The poll loop re-runs :meth:`_refresh` for
    #: every known announcement on an interval so the dns-sd fallback
    #: picks up the current TXT record from mDNSResponder's own cache.
    _POLL_INTERVAL_SEC = 1.0

    def __init__(self, session_id: Optional[str] = None) -> None:
        self._session_id_filter = session_id
        self._zc: Any = None
        self._browser: Any = None
        self._sessions: Dict[str, SessionAnnouncement] = {}
        # Names whose PTR record was observed (via ``add_service`` /
        # ``update_service``) but whose SRV/TXT has not yet been
        # successfully resolved. Tracked separately from ``_sessions``
        # so:
        #   1) :meth:`_poll_loop` can retry these at the poll interval,
        #      whereas previously it iterated ``_sessions.keys()`` only
        #      and a peer that never resolved was never retried.
        #   2) :meth:`pending_peer_names` can surface them to the UI
        #      as tentative entries ("resolving…") — the viewer's
        #      cluster panel shows them immediately on PTR observation
        #      instead of waiting for the full TXT resolution.
        self._pending_names: set[str] = set()
        self._lock = threading.Lock()
        self._update_event = threading.Condition(self._lock)
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the zeroconf instance and start browsing.

        Not idempotent: calling ``start()`` twice raises so misuse
        surfaces at the call site.
        """
        with self._lock:
            if self._zc is not None:
                raise RuntimeError("SessionBrowser already started")
            zc_factory = _get_zeroconf_cls()
            browser_factory = _get_service_browser_cls()
            self._zc = zc_factory()
            self._browser = browser_factory(self._zc, SERVICE_TYPE, self)
            self._poll_stop.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_loop,
                name=f"session-browser-poll-{self._session_id_filter or 'any'}",
                daemon=True,
            )
            self._poll_thread.start()
            logger.info(
                "SessionBrowser started (filter session_id=%s)",
                self._session_id_filter,
            )

    def close(self) -> None:
        """Cancel the service browser and close the ``Zeroconf`` instance.

        Safe to call multiple times — the second call is a no-op.
        """
        # Signal the poll thread to exit BEFORE we mutate protected state.
        # The thread blocks on self._poll_stop; setting it unblocks cleanly.
        self._poll_stop.set()
        with self._lock:
            if self._zc is None:
                return
            try:
                self._browser.cancel()
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning("ServiceBrowser.cancel failed: %s", exc)
            try:
                self._zc.close()
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning("Zeroconf.close failed: %s", exc)
            self._zc = None
            self._browser = None
        # Wake any threads blocked inside wait_for_observation /
        # wait_for_recording / wait_for_stopped so they can exit cleanly
        # instead of hanging on the condition variable forever (relevant
        # when a caller passed timeout=float("inf")).
        with self._update_event:
            self._update_event.notify_all()

    # ------------------------------------------------------------------
    # Public observation API
    # ------------------------------------------------------------------

    def current_sessions(self) -> List[SessionAnnouncement]:
        """Return a snapshot of every session the browser has observed."""
        with self._lock:
            return list(self._sessions.values())

    def wait_for_recording(self, timeout: float = 30.0) -> SessionAnnouncement:
        """Block until a matching leader advertises ``status="recording"``.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            The observed :class:`SessionAnnouncement`.

        Raises:
            TimeoutError: If no matching leader reaches ``"recording"``
                before the deadline.
        """
        return self._wait_for_status("recording", timeout)

    def wait_for_observation(self, timeout: float = 30.0) -> SessionAnnouncement:
        """Block until a matching leader is observed in ANY status.

        Faster than :meth:`wait_for_recording` — returns as soon as the
        browser has ANY matching announcement (preparing, recording, or
        stopped). Callers use this to detect a leader's presence without
        waiting for its session to actually start recording.

        Pass ``timeout=float("inf")`` to wait indefinitely — the
        follower's background observer thread uses this to sit on the
        condition variable for the entire duration of the connected
        session. In that mode no ``TimeoutError`` is ever raised; the
        only way out is an observation or a call to :meth:`close`.
        """
        deadline = (
            time.monotonic() + timeout if timeout != float("inf") else None
        )
        with self._update_event:
            while True:
                match = self._find_any_match()
                if match is not None:
                    return match
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"no matching leader observed within {timeout:.1f}s "
                            f"(filter session_id={self._session_id_filter!r})"
                        )
                    self._update_event.wait(timeout=remaining)
                else:
                    # Infinite wait — condition variable will wake us
                    # when the browser observes a matching announcement
                    # or when close() notifies waiters.
                    self._update_event.wait()

    def _find_any_match(self) -> Optional[SessionAnnouncement]:
        """Return any announcement matching the session_id filter, regardless of status.

        Caller must hold the condition lock.
        """
        for ann in self._sessions.values():
            if (
                self._session_id_filter is not None
                and ann.session_id != self._session_id_filter
            ):
                continue
            return ann
        return None

    def wait_for_stopped(self, timeout: float = 3600.0) -> SessionAnnouncement:
        """Block until a matching leader advertises ``status="stopped"``.

        Default timeout of one hour is intentionally generous — the
        follower is expected to stop when the leader stops, and a
        one-hour session is not unusual for teleop data collection.
        """
        return self._wait_for_status("stopped", timeout)

    # ------------------------------------------------------------------
    # Internal wait loop
    # ------------------------------------------------------------------

    def _wait_for_status(
        self, target_status: str, timeout: float
    ) -> SessionAnnouncement:
        """Block on the update condition until a match appears."""
        deadline = time.monotonic() + timeout
        with self._update_event:
            while True:
                match = self._find_match(target_status)
                if match is not None:
                    return match
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"no leader reached status={target_status!r} "
                        f"within {timeout:.1f}s "
                        f"(filter session_id={self._session_id_filter!r})"
                    )
                self._update_event.wait(timeout=remaining)

    def _find_match(self, target_status: str) -> Optional[SessionAnnouncement]:
        """Return an announcement matching the filter + target status.

        Caller must hold the condition lock.
        """
        for ann in self._sessions.values():
            if (
                self._session_id_filter is not None
                and ann.session_id != self._session_id_filter
            ):
                continue
            if ann.status == target_status:
                return ann
        return None

    # ------------------------------------------------------------------
    # zeroconf ServiceListener callbacks
    # ------------------------------------------------------------------

    def add_service(self, zc: Any, type_: str, name: str) -> None:
        logger.info("SessionBrowser add_service callback: %s", name)
        # Register as pending immediately so the UI + poll loop know
        # about the peer before TXT resolution completes. Only peers
        # that pass the session-id prefix filter matter; the filter is
        # re-checked inside :meth:`_refresh` as the single source of
        # truth so we don't duplicate it here.
        with self._update_event:
            if self._pending_matches_filter(name):
                self._pending_names.add(name)
                self._update_event.notify_all()
        self._refresh(zc, name)

    def update_service(self, zc: Any, type_: str, name: str) -> None:
        logger.info("SessionBrowser update_service callback: %s", name)
        self._refresh(zc, name)

    def remove_service(self, zc: Any, type_: str, name: str) -> None:
        with self._update_event:
            self._sessions.pop(name, None)
            self._pending_names.discard(name)
            self._update_event.notify_all()
        logger.info("SessionBrowser lost peer: %s", name)

    def _pending_matches_filter(self, name: str) -> bool:
        """Check the same session-id prefix filter ``_refresh`` uses.

        Peers in other sessions are ignored entirely — we don't want
        them cluttering the pending set or the cluster panel.
        """
        if self._session_id_filter is None:
            return True
        instance = name.split(".", 1)[0]
        return (
            instance == self._session_id_filter
            or instance.startswith(self._session_id_filter + "--")
        )

    def pending_peer_names(self) -> List[str]:
        """Return full mDNS instance names of peers whose TXT is still resolving.

        Used by the viewer's ``/api/cluster/peers`` endpoint to render
        a tentative "resolving…" row the moment a PTR record is
        observed, so the operator sees the peer is on the way instead
        of staring at a "no peers discovered yet" list while the dns-sd
        fallback grinds through cross-network SRV/TXT lookups.
        """
        with self._lock:
            return [n for n in self._pending_names if n not in self._sessions]

    # ------------------------------------------------------------------
    # Periodic re-resolution loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Periodically re-resolve every known peer.

        Works around python-zeroconf's unreliable ``update_service``
        callback on macOS: when a leader flips its TXT status from
        ``"preparing"`` to ``"recording"``, the multicast update often
        doesn't make it through to python-zeroconf's listener (the
        OS-level ``mDNSResponder`` intercepts unicast replies). By
        re-running :meth:`_refresh` on a timer, we force the dns-sd
        subprocess fallback to query ``mDNSResponder``'s local cache
        directly — which DID observe the update.

        Runs as a daemon thread spawned from :meth:`start` and exits
        cleanly when :meth:`close` sets ``self._poll_stop``.
        """
        while not self._poll_stop.is_set():
            # Sleep first so we don't race with the initial add_service
            # callbacks, which are already handling first resolution.
            if self._poll_stop.wait(self._POLL_INTERVAL_SEC):
                return

            with self._lock:
                zc = self._zc
                # Iterate BOTH resolved and pending peers. Previously we
                # only retried ``_sessions.keys()``, which meant a peer
                # whose first resolution failed (common on flaky WiFi)
                # was never re-attempted and stayed invisible forever.
                names = list(self._sessions.keys() | self._pending_names)
            if zc is None:
                return
            for name in names:
                if self._poll_stop.is_set():
                    return
                try:
                    self._refresh(zc, name)
                except Exception as exc:  # pragma: no cover - best-effort
                    logger.debug(
                        "SessionBrowser poll refresh failed for %s: %s",
                        name, exc,
                    )

    #: ``get_service_info`` timeout on non-macOS platforms, in
    #: milliseconds. zeroconf's listener callbacks fire as soon as the
    #: service name is known, sometimes before the TXT record has
    #: been fully resolved — waiting here gives the full resolution a
    #: chance before we return.
    _GET_INFO_TIMEOUT_MS = 5000

    #: ``get_service_info`` timeout on macOS. Shorter because macOS's
    #: mDNSResponder owns port 5353 and usually intercepts the unicast
    #: SRV/TXT replies zeroconf is waiting on, so zeroconf almost
    #: always returns None anyway. Waiting the full 5 s on every peer
    #: just blocks the poll loop — drop straight to the dns-sd
    #: subprocess fallback after 1.5 s instead.
    _GET_INFO_TIMEOUT_MS_DARWIN = 1500

    #: Timeout for the macOS ``dns-sd -L`` subprocess fallback, in
    #: seconds. Bumped from 5 s to give cross-network SRV/TXT queries
    #: room on slower WiFi — the 5 s ceiling was making the fallback
    #: consistently fail for a peer on a different machine even though
    #: mDNSResponder eventually had the data.
    _DNS_SD_FALLBACK_TIMEOUT_SEC = 10.0

    def _refresh(self, zc: Any, name: str) -> None:
        """Re-fetch the TXT record for *name* and update ``_sessions``.

        Any exception from the zeroconf call or the parser is logged
        and ignored — the browser must never crash on a single bad
        peer. The update condition is notified even when the refresh
        failed so waiters can re-evaluate their predicate.

        Uses :attr:`_GET_INFO_TIMEOUT_MS` as the blocking timeout on
        ``get_service_info`` so the callback waits for the full TXT
        record to resolve before returning. Tests that stub zeroconf
        with a synchronous fake backend can still call the same
        method with their fake ``get_service_info(type, name)`` —
        the keyword argument is forwarded via ``**kwargs`` so the
        fake only needs to accept what it cares about.
        """
        # Cheap session-id prefix filter: skip refresh entirely for
        # services that aren't part of our cluster. Production names
        # are "<session_id>--<host_id>" (SessionAdvertiser); some tests
        # register the bare "<session_id>" form, so accept both.
        # Skipping resolution for stale / other-cluster services avoids
        # minutes of stacked 5-second dns-sd subprocess timeouts on
        # busy networks.
        if self._session_id_filter is not None:
            instance = name.split(".", 1)[0]
            if not (
                instance == self._session_id_filter
                or instance.startswith(self._session_id_filter + "--")
            ):
                return
        logger.debug("_refresh: invoked for %s", name)

        from syncfield.multihost._dns_sd_fallback import (
            is_macos,
            resolve_via_dns_sd,
        )
        macos = is_macos()
        zc_timeout_ms = (
            self._GET_INFO_TIMEOUT_MS_DARWIN
            if macos
            else self._GET_INFO_TIMEOUT_MS
        )

        try:
            try:
                info = zc.get_service_info(
                    SERVICE_TYPE, name, timeout=zc_timeout_ms
                )
            except TypeError:
                # Fake backends in unit tests don't accept timeout —
                # retry without it so the same browser works against
                # both real zeroconf and the test doubles.
                info = zc.get_service_info(SERVICE_TYPE, name)
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("get_service_info failed for %s: %s", name, exc)
            info = None

        # Skip the zeroconf retry on macOS: mDNSResponder intercepts
        # unicast replies so retrying zeroconf's own query just burns
        # more timeout budget with the same None result. Go straight
        # to the dns-sd fallback (which talks to mDNSResponder's cache
        # directly). Non-macOS platforms keep the legacy retry.
        if info is None and not macos:
            logger.warning(
                "_refresh: first get_service_info returned None for %s; "
                "retrying...",
                name,
            )
            try:
                try:
                    info = zc.get_service_info(
                        SERVICE_TYPE, name, timeout=zc_timeout_ms
                    )
                except TypeError:
                    info = zc.get_service_info(SERVICE_TYPE, name)
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning(
                    "get_service_info retry failed for %s: %s", name, exc
                )
                info = None

        if info is None or not getattr(info, "properties", None):
            if macos:
                logger.debug(
                    "_refresh: zeroconf returned None/empty for %s; "
                    "using macOS dns-sd fallback...",
                    name,
                )
                info = resolve_via_dns_sd(
                    name,
                    SERVICE_TYPE,
                    timeout=self._DNS_SD_FALLBACK_TIMEOUT_SEC,
                )
                if info is None:
                    logger.debug(
                        "_refresh: macOS dns-sd fallback did not resolve %s "
                        "this round; will retry on next poll tick",
                        name,
                    )
                    return
                logger.info(
                    "_refresh: macOS dns-sd fallback resolved %s (port=%s)",
                    name,
                    info.port,
                )
            else:
                if info is None:
                    logger.warning(
                        "_refresh: get_service_info still None after retry "
                        "for %s — peer not resolved",
                        name,
                    )
                else:
                    logger.warning(
                        "_refresh: %s has empty TXT record properties", name
                    )
                return
        try:
            ann = SessionAnnouncement.from_txt_record(
                info.properties, last_seen_ns=time.monotonic_ns()
            )
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("bad announcement on %s: %s", name, exc)
            return
        # Translate the legacy sentinel (port=0) to None so downstream
        # code can distinguish "no control plane" from "port 0".
        raw_port = getattr(info, "port", None)
        port: Optional[int] = (
            raw_port if (raw_port is not None and raw_port > 0) else None
        )
        ann = replace(ann, control_plane_port=port)
        address = _extract_ipv4(info)
        ann = replace(ann, resolved_address=address)
        with self._update_event:
            previous = self._sessions.get(name)
            self._sessions[name] = ann
            # Peer is now fully resolved; drop any pending placeholder.
            self._pending_names.discard(name)
            self._update_event.notify_all()
        if previous is None:
            logger.info(
                "SessionBrowser observed peer: host_id=%s status=%s "
                "control_plane_port=%s",
                ann.host_id,
                ann.status,
                ann.control_plane_port,
            )
        elif previous.status != ann.status:
            logger.info(
                "SessionBrowser peer status change: host_id=%s %s -> %s",
                ann.host_id,
                previous.status,
                ann.status,
            )


def _extract_ipv4(info: Any) -> Optional[str]:
    """Return the first dialable address on *info*, or ``None``.

    Prefers a dotted-quad IPv4 (from ``info.parsed_addresses()`` or the
    legacy ``info.addresses`` packed-bytes list). Falls back to a
    ``.local`` mDNS hostname when no IPv4 was resolvable: macOS's
    getaddrinfo routes ``.local`` lookups through mDNSResponder, so
    httpx and the standard library can still dial it. Without this
    fallback, a dns-sd-only resolution with failed hostname→IPv4
    step would drop ``resolved_address`` to ``None`` and every
    downstream POST/GET against the peer would silently redirect to
    ``127.0.0.1`` via :meth:`SessionOrchestrator._follower_base_url`.

    Any exception short-circuits to ``None`` rather than crashing the
    browser — address resolution is best-effort and callers handle the
    missing-address case.
    """
    try:
        parsed = info.parsed_addresses()
    except AttributeError:
        parsed = None
    except Exception as exc:  # pragma: no cover - best-effort
        logger.warning("parsed_addresses() failed: %s", exc)
        parsed = None

    if parsed:
        for addr in parsed:
            if isinstance(addr, str) and addr.count(".") == 3:
                return addr

    raw = getattr(info, "addresses", None)
    if raw:
        import socket

        for packed in raw:
            if isinstance(packed, (bytes, bytearray)) and len(packed) == 4:
                try:
                    return socket.inet_ntoa(bytes(packed))
                except OSError:  # pragma: no cover - malformed packed addr
                    continue

    # Hostname-only fallback: dns-sd fallback's _ResolvedInfo exposes a
    # normalised ``.local`` hostname when it couldn't resolve the peer
    # to packed IPv4. httpx + macOS getaddrinfo can dial that directly
    # via mDNSResponder.
    hostname = getattr(info, "hostname", None)
    if isinstance(hostname, str) and hostname:
        return hostname
    return None
