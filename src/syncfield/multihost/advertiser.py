"""mDNS service registration for SyncField session leaders.

A :class:`SessionAdvertiser` wraps a single ``python-zeroconf`` service
registration. Leaders construct one, call :meth:`start` before opening
streams, flip the status to ``"recording"`` right before the start
chirp via :meth:`update_status`, and flip it to ``"stopped"`` right
after the stop chirp. Close with :meth:`close`.

The ``zeroconf`` import is lazy and happens inside ``_get_zeroconf_cls``
so tests can monkey-patch it and so machines without the ``multihost``
extra installed can still import ``syncfield.multihost.advertiser`` —
they only crash when actually trying to start advertising.

Thread safety: a single lock serializes all state mutations so the
orchestrator's RLock doesn't have to care about discovery internals.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any, Callable, Optional

from syncfield.multihost.naming import is_valid_session_id
from syncfield.multihost.types import SessionAdvertStatus, SessionAnnouncement

logger = logging.getLogger(__name__)

#: mDNS service type used by every SyncField session advertisement.
SERVICE_TYPE = "_syncfield._tcp.local."

#: Legacy sentinel used when the host exposes no control plane. When
#: :class:`SessionAdvertiser` is constructed without ``control_plane_port``,
#: this value is published as ``ServiceInfo.port``; the browser
#: translates it back to ``None``. When ``control_plane_port`` is
#: supplied, the real port is published instead — the mDNS port field
#: is now meaningful, not just a formality.
ADVERT_PORT = 0


def _get_zeroconf_cls() -> Callable[[], Any]:
    """Return a zero-argument factory for a ``Zeroconf`` instance.

    Isolated as a helper so unit tests can monkey-patch it with a fake
    backend without needing the real library installed in the test
    environment.
    """
    from zeroconf import Zeroconf  # type: ignore[import-not-found]

    return Zeroconf


def _get_service_info_cls() -> Callable[..., Any]:
    """Return a factory for ``ServiceInfo``. See :func:`_get_zeroconf_cls`."""
    from zeroconf import ServiceInfo  # type: ignore[import-not-found]

    return ServiceInfo


class SessionAdvertiser:
    """Advertises one SyncField session on the local network via mDNS.

    One advertiser corresponds to one leader-side
    :class:`~syncfield.orchestrator.SessionOrchestrator`. The advertiser
    owns its own ``Zeroconf`` instance — do not share one across
    orchestrators on the same process.

    Args:
        session_id: Shared identifier for this session. Must pass
            :func:`~syncfield.multihost.naming.is_valid_session_id`.
        host_id: The leader's host id. Stored in the TXT record so
            followers can correlate against the manifest after the
            session.
        sdk_version: SyncField SDK version string — typically obtained
            via ``importlib.metadata.version("syncfield")``.
        chirp_enabled: Whether the leader will play sync chirps during
            this session. Followers read this to know whether to
            expect an audio anchor or fall back to timestamp alignment.
        graceful_shutdown_ms: How long :meth:`close` keeps broadcasting
            the final ``"stopped"`` status before unregistering the
            service. Default ``1000`` ms — enough for any follower on
            the same network to receive the update and begin stopping.
        control_plane_port: TCP port the host's HTTP control plane
            serves on. Published as the mDNS ``ServiceInfo.port`` so
            followers can discover it without a second round-trip.
            ``None`` (default) falls back to the legacy sentinel ``0``
            for callers that do not run a control plane.
    """

    def __init__(
        self,
        session_id: str,
        host_id: str,
        sdk_version: str,
        chirp_enabled: bool,
        graceful_shutdown_ms: int = 1000,
        control_plane_port: Optional[int] = None,
    ) -> None:
        if not is_valid_session_id(session_id):
            raise ValueError(
                f"session_id {session_id!r} is not a valid slug; "
                "use generate_session_id() or match [a-zA-Z0-9_-]{1,64}"
            )
        self._announcement = SessionAnnouncement(
            session_id=session_id,
            host_id=host_id,
            status="preparing",
            sdk_version=sdk_version,
            chirp_enabled=chirp_enabled,
            control_plane_port=control_plane_port,
        )
        self._graceful_shutdown_ms = graceful_shutdown_ms
        self._zc: Any = None
        self._info: Any = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._announcement.session_id

    @property
    def announcement(self) -> SessionAnnouncement:
        """Return the most recent announcement the advertiser is broadcasting."""
        return self._announcement

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open a ``Zeroconf`` instance and register the service.

        Not idempotent: calling ``start()`` twice raises so misuse
        surfaces immediately instead of leaking a silent second
        registration.
        """
        with self._lock:
            if self._zc is not None:
                raise RuntimeError("SessionAdvertiser already started")
            zc_factory = _get_zeroconf_cls()
            self._zc = zc_factory()
            self._info = self._build_service_info(self._announcement)
            self._zc.register_service(self._info)
            logger.info(
                "SessionAdvertiser started: session_id=%s host_id=%s",
                self._announcement.session_id,
                self._announcement.host_id,
            )

    def update_status(
        self,
        status: SessionAdvertStatus,
        *,
        started_at_ns: Optional[int] = None,
    ) -> None:
        """Transition the advertised status.

        Builds a **new** ``ServiceInfo`` with the updated TXT record
        and passes it to ``Zeroconf.update_service``. We don't mutate
        the existing ``ServiceInfo`` in place because ``properties``
        is read-only on ``zeroconf>=0.140`` — mutating it used to
        work silently on older versions, which was brittle.

        Args:
            status: New lifecycle phase.
            started_at_ns: Optional monotonic ns to embed in the TXT
                record alongside the ``"recording"`` transition. When
                omitted, the previously stored value (if any) is
                preserved so an intermediate ``"stopped"`` transition
                doesn't erase a prior ``started_at``.

        Raises:
            RuntimeError: If called before :meth:`start`.
        """
        with self._lock:
            if self._zc is None or self._info is None:
                raise RuntimeError("SessionAdvertiser not started")
            self._announcement = SessionAnnouncement(
                session_id=self._announcement.session_id,
                host_id=self._announcement.host_id,
                status=status,
                sdk_version=self._announcement.sdk_version,
                chirp_enabled=self._announcement.chirp_enabled,
                started_at_ns=(
                    started_at_ns
                    if started_at_ns is not None
                    else self._announcement.started_at_ns
                ),
                control_plane_port=self._announcement.control_plane_port,
            )
            self._info = self._build_service_info(self._announcement)
            self._zc.update_service(self._info)
            logger.info(
                "SessionAdvertiser status=%s (session_id=%s)",
                status,
                self._announcement.session_id,
            )

    def _build_service_info(self, announcement: SessionAnnouncement) -> Any:
        """Construct a ``ServiceInfo`` for the given announcement.

        Both :meth:`start` (initial registration) and
        :meth:`update_status` (every status transition) call this so
        the TXT record is always built via the public constructor
        rather than through private attribute mutation.
        """
        info_cls = _get_service_info_cls()
        port = (
            announcement.control_plane_port
            if announcement.control_plane_port is not None
            else ADVERT_PORT
        )
        return info_cls(
            SERVICE_TYPE,
            f"{announcement.session_id}.{SERVICE_TYPE}",
            port=port,
            properties=announcement.to_txt_record(),
            server=f"{socket.gethostname()}.local.",
        )

    def close(self) -> None:
        """Unregister the service and close the ``Zeroconf`` instance.

        Sleeps for ``graceful_shutdown_ms`` before unregistering so any
        follower still attached to the network observes the final
        status transition. Safe to call multiple times — the second
        call is a no-op.
        """
        with self._lock:
            if self._zc is None:
                return
            if self._graceful_shutdown_ms > 0:
                time.sleep(self._graceful_shutdown_ms / 1000.0)
            try:
                self._zc.unregister_service(self._info)
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning("unregister_service failed: %s", exc)
            try:
                self._zc.close()
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning("Zeroconf.close failed: %s", exc)
            self._zc = None
            self._info = None
