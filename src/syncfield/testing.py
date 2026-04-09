"""Testing utilities — programmable Stream implementations for unit tests.

This module is part of the **public** SyncField API surface so that
third-party adapter authors can reuse these helpers when testing their own
orchestrator integrations. Nothing here is marked private.
"""

from __future__ import annotations

from typing import Optional

from syncfield.clock import SessionClock
from syncfield.stream import StreamBase
from syncfield.types import (
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    StreamCapabilities,
)


class FakeStream(StreamBase):
    """Programmable in-memory :class:`~syncfield.stream.Stream` used by tests.

    Tracks lifecycle call counts and lets the test driver push samples and
    health events through the standard callback path. Supports failure
    injection via the three ``fail_on_*`` flags so tests can exercise
    orchestrator rollback, best-effort stop, and error reporting.

    Args:
        id: Stream id.
        provides_audio_track: Whether this stream reports audio capability
            (used to exercise chirp eligibility in orchestrator tests).
        fail_on_prepare: If ``True``, ``prepare()`` raises ``RuntimeError``.
        fail_on_start: If ``True``, ``start()`` raises ``RuntimeError``.
        fail_on_stop: If ``True``, ``stop()`` returns a failed
            :class:`FinalizationReport` instead of raising.
    """

    def __init__(
        self,
        id: str,
        provides_audio_track: bool = False,
        fail_on_prepare: bool = False,
        fail_on_start: bool = False,
        fail_on_stop: bool = False,
    ) -> None:
        super().__init__(
            id=id,
            kind="custom",
            capabilities=StreamCapabilities(
                provides_audio_track=provides_audio_track,
                supports_precise_timestamps=True,
                is_removable=False,
                produces_file=False,
            ),
        )
        self.prepare_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self._fail_on_prepare = fail_on_prepare
        self._fail_on_start = fail_on_start
        self._fail_on_stop = fail_on_stop
        self._frame_count = 0
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None

    # --- Stream SPI --------------------------------------------------------

    def prepare(self) -> None:
        self.prepare_calls += 1
        if self._fail_on_prepare:
            raise RuntimeError("fake failure in prepare")

    def start(self, session_clock: SessionClock) -> None:
        self.start_calls += 1
        if self._fail_on_start:
            raise RuntimeError("fake failure in start")

    def stop(self) -> FinalizationReport:
        self.stop_calls += 1
        status: str = "failed" if self._fail_on_stop else "completed"
        error: Optional[str] = "fake failure in stop" if self._fail_on_stop else None
        return FinalizationReport(
            stream_id=self.id,
            status=status,  # type: ignore[arg-type]
            frame_count=self._frame_count,
            file_path=None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=error,
        )

    # --- Test-only driving API (not part of the Stream SPI) ---------------

    def push_sample(self, frame_number: int, capture_ns: int) -> None:
        """Emit a synthetic sample through the orchestrator's callback path."""
        if self._first_at is None:
            self._first_at = capture_ns
        self._last_at = capture_ns
        self._frame_count += 1
        self._emit_sample(SampleEvent(self.id, frame_number, capture_ns))

    def push_health(
        self,
        kind: HealthEventKind,
        at_ns: int,
        detail: Optional[str] = None,
    ) -> None:
        """Emit a synthetic health event through the callback path."""
        self._emit_health(HealthEvent(self.id, kind, at_ns, detail))
