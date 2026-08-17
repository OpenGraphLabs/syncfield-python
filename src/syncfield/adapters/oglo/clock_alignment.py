"""Project OGLO device timestamps into the host monotonic clock domain.

USB CDC delivers several independently timestamped TAG frames in one tty read.
Timestamping each decoded frame at parser time collapses their real spacing to
microseconds and makes a 4 ms tactile clock look bursty.  The projector anchors
the newest device timestamp in a read batch to the host read-completion time,
then uses the device deltas for every frame in that batch.

The smallest observed ``host_receive - device`` offset is retained for the
connection.  A later kernel/tty backlog can therefore make a batch arrive late,
but cannot drag its acquisition timestamps forward with that backlog.  This is
the usual one-way, minimum-delay clock projection: it preserves the device
timeline and chooses the observation with the least host-side delay as the
clock-domain anchor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class HostDeviceClockProjector:
    """Stateful minimum-delay projection for one physical device epoch."""

    _offset_ns: int | None = None
    _last_capture_ns: dict[str, int] = field(default_factory=dict)

    def reset(self) -> None:
        """Forget the old MCU/connection epoch."""
        self._offset_ns = None
        self._last_capture_ns.clear()

    @property
    def offset_ns(self) -> int | None:
        return self._offset_ns

    def project_batch(
        self,
        samples: Iterable[tuple[str, int]],
        *,
        receive_ns: int,
    ) -> tuple[int, ...]:
        """Return host-monotonic acquisition times for one receive batch.

        ``samples`` contains ``(modality, unwrapped_device_ns)`` pairs in wire
        order.  The result has the same length and is strictly increasing
        within each modality, even if a newly observed lower-delay anchor would
        otherwise step an already-published timeline backward.
        """
        materialized = tuple((str(name), int(device_ns)) for name, device_ns in samples)
        if not materialized:
            return ()

        latest_device_ns = max(device_ns for _, device_ns in materialized)
        candidate_offset_ns = int(receive_ns) - latest_device_ns
        if self._offset_ns is None or candidate_offset_ns < self._offset_ns:
            self._offset_ns = candidate_offset_ns

        assert self._offset_ns is not None
        projected: list[int] = []
        for modality, device_ns in materialized:
            capture_ns = device_ns + self._offset_ns
            previous = self._last_capture_ns.get(modality)
            if previous is not None and capture_ns <= previous:
                capture_ns = previous + 1
            # A device sample cannot be acquired after the host completed the
            # read that contained it.  This also makes corrupt/future clocks
            # fail visibly in tests instead of leaking future timestamps.
            capture_ns = min(capture_ns, int(receive_ns))
            self._last_capture_ns[modality] = capture_ns
            projected.append(capture_ns)
        return tuple(projected)
