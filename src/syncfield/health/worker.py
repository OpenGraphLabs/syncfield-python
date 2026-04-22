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
from dataclasses import dataclass
from typing import Iterable, List

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


class HealthWorker:
    def __init__(
        self,
        *,
        tracker: IncidentTracker,
        detectors: Iterable[Detector],
        tick_hz: float = 20.0,
    ) -> None:
        self._tracker = tracker
        self._detectors: List[Detector] = list(detectors)
        self._tick_interval = 1.0 / tick_hz

        self._samples: "queue.SimpleQueue[_SampleMsg]" = queue.SimpleQueue()
        self._healths: "queue.SimpleQueue[_HealthMsg]" = queue.SimpleQueue()
        self._states: "queue.SimpleQueue[_StateMsg]" = queue.SimpleQueue()
        self._writer_stats: "queue.SimpleQueue[_WriterStatsMsg]" = queue.SimpleQueue()

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
            sleep_for = next_deadline - time.monotonic()
            if sleep_for > 0:
                # Use Event.wait so stop() can interrupt immediately.
                self._stop.wait(timeout=sleep_for)
            else:
                # Running behind; reset the schedule anchor.
                next_deadline = time.monotonic()

        # Drain any straggling messages after stop so tests see a consistent state.
        self._drain_once()

    def _drain_once(self) -> None:
        for msg in _drain_queue(self._samples):
            for d in self._detectors:
                d.observe_sample(msg.stream_id, msg.sample)
        for msg in _drain_queue(self._healths):
            for d in self._detectors:
                d.observe_health(msg.stream_id, msg.event)
            self._tracker.ingest(msg.event)
        for msg in _drain_queue(self._states):
            for d in self._detectors:
                d.observe_state(msg.old, msg.new)
        for msg in _drain_queue(self._writer_stats):
            for d in self._detectors:
                d.observe_writer_stats(msg.stream_id, msg.stats)

    def _fire_detector_ticks(self) -> None:
        now = time.monotonic_ns()
        for d in self._detectors:
            for event in d.tick(now):
                self._tracker.ingest(event)


def _drain_queue(q: "queue.SimpleQueue") -> List:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out
