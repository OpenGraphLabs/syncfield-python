"""HealthWorker — the dedicated thread that drives detectors + tracker.

Capture threads push samples / health events / state transitions /
writer stats into :class:`queue.SimpleQueue`\\ s. The worker drains them
every tick, fans out to each registered Detector, runs each Detector's
``tick`` to emit synthetic events, and feeds everything into the
IncidentTracker.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from syncfield.health.detector import Detector
from syncfield.health.tracker import IncidentTracker
from syncfield.health.types import WriterStats
from syncfield.types import HealthEvent, SampleEvent, SessionState


@dataclass(frozen=True)
class _SampleMsg:
    stream_id: str
    sample: SampleEvent


@dataclass(frozen=True)
class _HealthMsg:
    stream_id: str
    event: HealthEvent


@dataclass(frozen=True)
class _StateMsg:
    old: SessionState
    new: SessionState


@dataclass(frozen=True)
class _WriterStatsMsg:
    stream_id: str
    stats: WriterStats


@dataclass(frozen=True)
class _ConnectionStateMsg:
    stream_id: str
    new_state: str
    at_ns: int


class HealthWorker:
    def __init__(
        self,
        *,
        tracker: IncidentTracker,
        detectors: Iterable[Detector],
        tick_hz: float = 20.0,
    ) -> None:
        self._tracker = tracker
        self._detectors: list[Detector] = list(detectors)
        self._tick_interval = 1.0 / tick_hz

        # SimpleQueue is unbounded. Producer rate is bounded in practice by
        # hardware frame rate (tens of Hz) and the worker drains at tick_hz
        # (default 20 Hz) plus post-stop. No back-pressure is needed for the
        # intended load; if that changes, swap to queue.Queue with maxsize.
        self._samples: "queue.SimpleQueue[_SampleMsg]" = queue.SimpleQueue()
        self._healths: "queue.SimpleQueue[_HealthMsg]" = queue.SimpleQueue()
        self._states: "queue.SimpleQueue[_StateMsg]" = queue.SimpleQueue()
        self._writer_stats: "queue.SimpleQueue[_WriterStatsMsg]" = queue.SimpleQueue()
        self._connection_states: "queue.SimpleQueue[_ConnectionStateMsg]" = queue.SimpleQueue()

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- ingress (called from capture threads) ---------------------------

    def push_sample(self, stream_id: str, sample: SampleEvent) -> None:
        self._samples.put(_SampleMsg(stream_id, sample))

    def push_health(self, stream_id: str, event: HealthEvent) -> None:
        self._healths.put(_HealthMsg(stream_id, event))

    def push_state(self, old: SessionState, new: SessionState) -> None:
        self._states.put(_StateMsg(old, new))

    def push_writer_stats(self, stream_id: str, stats: WriterStats) -> None:
        self._writer_stats.put(_WriterStatsMsg(stream_id, stats))

    def push_connection_state(self, stream_id: str, new_state: str, at_ns: int) -> None:
        self._connection_states.put(_ConnectionStateMsg(stream_id, new_state, at_ns))

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="syncfield-health", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    # --- main loop -------------------------------------------------------

    def _run(self) -> None:
        next_deadline = time.monotonic()
        while not self._stop.is_set():
            self._drain_once()
            self._fire_detector_ticks()
            self._tracker.tick(now_ns=time.monotonic_ns())

            next_deadline += self._tick_interval
            now = time.monotonic()
            sleep_for = next_deadline - now
            if sleep_for > 0:
                # Event.wait lets stop() cut short the sleep.
                self._stop.wait(timeout=sleep_for)
            else:
                # Running behind; reset anchor to the clock reading we just took.
                next_deadline = now

        # Drain any stragglers so post-stop state is consistent. We deliberately
        # do NOT call tracker.tick() here — incidents still open at stop time
        # are resolved by SessionOrchestrator via IncidentTracker.close_all(),
        # not by one last opportunistic close_condition pass.
        self._drain_once()

    def _drain_once(self) -> None:
        for msg in _drain_queue(self._samples):
            for d in self._detectors:
                d.observe_sample(msg.stream_id, msg.sample)
        for msg in _drain_queue(self._healths):
            for d in self._detectors:
                d.observe_health(msg.stream_id, msg.event)
            self._safe_ingest(msg.event)
        for msg in _drain_queue(self._states):
            for d in self._detectors:
                d.observe_state(msg.old, msg.new)
        for msg in _drain_queue(self._writer_stats):
            for d in self._detectors:
                d.observe_writer_stats(msg.stream_id, msg.stats)
        for msg in _drain_queue(self._connection_states):
            for d in self._detectors:
                d.observe_connection_state(msg.stream_id, msg.new_state, msg.at_ns)

    def _fire_detector_ticks(self) -> None:
        now = time.monotonic_ns()
        for d in self._detectors:
            for event in d.tick(now):
                self._safe_ingest(event)

    def _safe_ingest(self, event: HealthEvent) -> None:
        """Ingest but never let a malformed event crash the worker thread."""
        try:
            self._tracker.ingest(event)
        except Exception as exc:   # noqa: BLE001 — telemetry must not crash
            import logging
            logging.getLogger(__name__).warning(
                "IncidentTracker.ingest dropped event: %s (fingerprint=%r, source=%r)",
                exc, event.fingerprint, event.source,
            )


def _drain_queue(q: "queue.SimpleQueue") -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out
