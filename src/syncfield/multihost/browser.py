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
from typing import Any, Callable, Dict, List, Optional

from syncfield.multihost.advertiser import SERVICE_TYPE
from syncfield.multihost.types import SessionAnnouncement

logger = logging.getLogger(__name__)


def _get_zeroconf_cls() -> Callable[[], Any]:
    """Return a zero-argument factory for a ``Zeroconf`` instance."""
    from zeroconf import Zeroconf  # type: ignore[import-not-found]

    return Zeroconf


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

    def __init__(self, session_id: Optional[str] = None) -> None:
        self._session_id_filter = session_id
        self._zc: Any = None
        self._browser: Any = None
        self._sessions: Dict[str, SessionAnnouncement] = {}
        self._lock = threading.Lock()
        self._update_event = threading.Condition(self._lock)

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
            logger.info(
                "SessionBrowser started (filter session_id=%s)",
                self._session_id_filter,
            )

    def close(self) -> None:
        """Cancel the service browser and close the ``Zeroconf`` instance.

        Safe to call multiple times — the second call is a no-op.
        """
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
        self._refresh(zc, name)

    def update_service(self, zc: Any, type_: str, name: str) -> None:
        self._refresh(zc, name)

    def remove_service(self, zc: Any, type_: str, name: str) -> None:
        with self._update_event:
            self._sessions.pop(name, None)
            self._update_event.notify_all()

    def _refresh(self, zc: Any, name: str) -> None:
        """Re-fetch the TXT record for *name* and update ``_sessions``.

        Any exception from the zeroconf call or the parser is logged
        and ignored — the browser must never crash on a single bad
        peer. The update condition is notified even when the refresh
        failed so waiters can re-evaluate their predicate.
        """
        try:
            info = zc.get_service_info(SERVICE_TYPE, name)
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("get_service_info failed for %s: %s", name, exc)
            return
        if info is None or not getattr(info, "properties", None):
            return
        try:
            ann = SessionAnnouncement.from_txt_record(
                info.properties, last_seen_ns=time.monotonic_ns()
            )
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("bad announcement on %s: %s", name, exc)
            return
        with self._update_event:
            self._sessions[name] = ann
            self._update_event.notify_all()
