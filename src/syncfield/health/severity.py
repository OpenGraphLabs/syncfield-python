"""Severity levels for health events and incidents.

Ordered INFO < WARNING < ERROR < CRITICAL. Use :func:`max_severity` to
pick the highest of several levels — incidents escalate to the max
severity of their constituent events.
"""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _RANK[self]


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
