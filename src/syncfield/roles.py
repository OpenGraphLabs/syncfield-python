"""Multi-host role configuration for :class:`SessionOrchestrator`.

Two roles participate in a multi-host SyncField session on a shared
local network:

- :class:`LeaderRole` — the host that owns the primary stream, plays
  the sync chirps, and advertises the session via
  :mod:`syncfield.multihost` so followers can discover it.
- :class:`FollowerRole` — any other host on the same network; it
  browses for the leader's advertisement, blocks until the leader is
  recording, starts its own streams without playing chirps, and stops
  when the leader broadcasts ``"stopped"`` (or when the user calls
  ``stop()`` explicitly).

Single-host sessions construct a :class:`SessionOrchestrator` without
a role — the role-aware behavior is strictly opt-in so existing
callers see no behavioral change.

These dataclasses are deliberately tiny: they carry configuration
only, no state. State lives in the orchestrator and the multihost
advertiser/browser. That separation keeps the role types cheaply
copyable and safe to inspect from test code or logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from syncfield.multihost.naming import generate_session_id, is_valid_session_id

RoleKind = Literal["leader", "follower"]


@dataclass
class LeaderRole:
    """Configuration for the leader side of a multi-host session.

    Attributes:
        session_id: Shared session identifier. Auto-generated as a
            Docker-style slug (via
            :func:`~syncfield.multihost.naming.generate_session_id`)
            when not supplied so a quickstart script can run without
            any manual id coordination. Explicit ids must pass
            :func:`~syncfield.multihost.naming.is_valid_session_id`.
        graceful_shutdown_ms: How long the advertiser keeps
            broadcasting ``status="stopped"`` before unregistering so
            followers on the same network observe the transition.
            Default ``1000`` ms, which is comfortable for any
            real-world mDNS propagation and barely perceptible in
            manual workflows.
    """

    session_id: Optional[str] = None
    graceful_shutdown_ms: int = 1000

    #: TCP port the control plane will prefer when it binds. ``7878``
    #: matches ``syncfield.multihost.control_plane.DEFAULT_CONTROL_PLANE_PORT``
    #: (hardcoded here to keep ``roles`` free of FastAPI imports; the
    #: invariant is pinned by a tests/unit/test_roles.py assertion).
    control_plane_port: int = 7878

    #: Seconds the control plane stays up after ``stop()`` so the leader
    #: can pull files from followers. Default mirrors
    #: ``syncfield.multihost.control_plane.DEFAULT_KEEP_ALIVE_AFTER_STOP_SEC``.
    keep_alive_after_stop_sec: float = 600.0

    def __post_init__(self) -> None:
        if self.session_id is None:
            self.session_id = generate_session_id()
        elif not is_valid_session_id(self.session_id):
            raise ValueError(
                f"session_id {self.session_id!r} is not a valid slug; "
                "use generate_session_id() or match [a-zA-Z0-9_-]{1,64}"
            )

    @property
    def kind(self) -> RoleKind:
        return "leader"


@dataclass
class FollowerRole:
    """Configuration for the follower side of a multi-host session.

    Attributes:
        session_id: Optional session id filter. When set, the follower
            only joins a leader whose advertisement matches. When
            ``None``, the follower joins the first leader it observes
            in the ``"recording"`` state — suitable for single-leader
            labs where the operator does not want to type an id.
        leader_wait_timeout_sec: Maximum time
            :meth:`SessionOrchestrator.start` blocks waiting for a
            leader to reach ``status="recording"``. Default ``60`` s —
            enough for a human operator to start the leader after the
            follower without rushing.
    """

    session_id: Optional[str] = None
    leader_wait_timeout_sec: float = 60.0

    #: TCP port the control plane will prefer when it binds. ``7878``
    #: matches ``syncfield.multihost.control_plane.DEFAULT_CONTROL_PLANE_PORT``
    #: (hardcoded here to keep ``roles`` free of FastAPI imports; the
    #: invariant is pinned by a tests/unit/test_roles.py assertion).
    control_plane_port: int = 7878

    #: Seconds the control plane stays up after ``stop()`` so the leader
    #: can pull files from followers. Default mirrors
    #: ``syncfield.multihost.control_plane.DEFAULT_KEEP_ALIVE_AFTER_STOP_SEC``.
    keep_alive_after_stop_sec: float = 600.0

    def __post_init__(self) -> None:
        if self.session_id is not None and not is_valid_session_id(
            self.session_id
        ):
            raise ValueError(
                f"session_id {self.session_id!r} is not a valid slug"
            )

    @property
    def kind(self) -> RoleKind:
        return "follower"
