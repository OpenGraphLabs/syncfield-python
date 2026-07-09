"""Severity levels for health events and incidents.

Ordered INFO < WARNING < ERROR < CRITICAL. Use :func:`max_severity` to
pick the highest of several levels — incidents escalate to the max
severity of their constituent events.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from syncfield.types import HealthEventKind


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _RANK[self]


# `(str, Enum)` inherits alphabetical string comparison, which would order
# critical < error < info < warning — wrong for severity. `_RANK` pins the
# intended order explicitly and gives `.rank` O(1) lookup.
_RANK = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.ERROR: 2,
    Severity.CRITICAL: 3,
}


def max_severity(*levels: Severity) -> Severity:
    if not levels:
        raise ValueError("max_severity requires at least one Severity")
    return max(levels, key=lambda s: s.rank)


def severity_for_kind(kind: "HealthEventKind") -> Severity:
    """Return the platform's default :class:`Severity` for a health event kind.

    This is the canonical mapping the platform uses to fill in
    ``HealthEvent.severity`` for adapter-emitted events that left it at the
    ``Severity.INFO`` default (see :class:`~syncfield.types.HealthEvent` and
    :meth:`~syncfield.stream.StreamBase._emit_health`). ``HEARTBEAT`` and
    ``RECONNECT`` are informational; ``DROP`` and ``WARNING`` deserve a
    human's attention; ``ERROR`` is unambiguous. Anything else defaults to
    ``WARNING`` — safer to over-report than to silently swallow an unknown
    kind at ``INFO``.

    Imports :class:`~syncfield.types.HealthEventKind` locally to avoid a
    circular import: ``syncfield.types`` imports :class:`Severity` from this
    module at module scope.
    """
    from syncfield.types import HealthEventKind

    mapping = {
        HealthEventKind.HEARTBEAT: Severity.INFO,
        HealthEventKind.RECONNECT: Severity.INFO,
        HealthEventKind.DROP: Severity.WARNING,
        HealthEventKind.WARNING: Severity.WARNING,
        HealthEventKind.ERROR: Severity.ERROR,
    }
    return mapping.get(kind, Severity.WARNING)
