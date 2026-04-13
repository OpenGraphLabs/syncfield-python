"""Exception types shared across the multi-host machinery."""

from __future__ import annotations

from typing import Dict, List


class ClusterConfigMismatch(Exception):
    """Raised when one or more followers reject the leader's session config.

    Accumulates per-host rejection reasons so the operator sees the full
    cluster picture in one error — not just the first failure.

    Attributes:
        rejections: Mapping of ``host_id`` → human-readable reason for
            every follower that returned 400. An empty mapping is not a
            valid construction (would mean "no mismatch").
    """

    def __init__(self, rejections: Dict[str, str]) -> None:
        if not rejections:
            raise ValueError("ClusterConfigMismatch requires at least one rejection")
        self.rejections = dict(rejections)
        lines: List[str] = [
            f"Cluster config rejected by {len(rejections)} follower(s):"
        ] + [f"  - {host}: {reason}" for host, reason in rejections.items()]
        super().__init__("\n".join(lines))
