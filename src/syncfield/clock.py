"""SessionClock — immutable clock handle distributed to all Streams in a session.

Captured once by :class:`syncfield.orchestrator.SessionOrchestrator` at
``start()`` and passed to every ``Stream.start()`` call. Provides the session's
:class:`syncfield.types.SyncPoint` together with a small helper API so
individual streams never need to import :mod:`time` or touch the session
anchor directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from syncfield.types import SyncPoint


@dataclass(frozen=True)
class SessionClock:
    """Shared monotonic clock reference for all streams in one session.

    A ``SessionClock`` is cheap to copy, safe to share across threads, and
    binds each stream in a session to the exact same monotonic anchor. The
    orchestrator constructs it once at ``start()`` and distributes it to
    every ``Stream.start(session_clock)`` call so intra-session timing uses
    a single source of truth.

    Attributes:
        sync_point: The session's :class:`SyncPoint` (monotonic + wall clock
            anchor captured at session start).
        recording_armed_ns: Common host monotonic_ns captured by the
            orchestrator right before it fans out ``start_recording()``
            to every stream. ``None`` during preview phase, non-``None``
            once recording is armed. All streams receive the same value,
            so adapters can use it as a shared intra-host sync anchor.
    """

    sync_point: SyncPoint
    recording_armed_ns: int | None = None

    @property
    def host_id(self) -> str:
        """Host identifier for this session."""
        return self.sync_point.host_id

    def now_ns(self) -> int:
        """Return the current monotonic nanosecond timestamp.

        Thread-safe: :func:`time.monotonic_ns` is atomic on CPython.
        """
        return time.monotonic_ns()

    def elapsed_ns(self) -> int:
        """Return nanoseconds elapsed since the session's sync point."""
        return time.monotonic_ns() - self.sync_point.monotonic_ns
