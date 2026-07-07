"""Stream supervision — the reliable health state machine + reconnect driver.

This module owns the *decision* half of stream reliability. It is deliberately
free of threads, sleeps, and device I/O so it can be unit-tested purely by
driving explicit lifecycle notes and ``tick(now_ns)`` calls with a fake clock.

Two pieces live here:

- :class:`ReconnectPolicy` — a pure, immutable knob set (enabled?, how long to
  back off before attempt N). Default is *disabled*, so a session that never
  configures a policy behaves exactly as it did before this module existed.
- :class:`StreamSupervisor` — a per-stream health state machine (see
  :class:`~syncfield.types.StreamConnectionState`) that turns raw signals
  (lifecycle transitions, sample arrivals, capture-loop deaths) into a small
  set of **transition-only** status/health events, detects stalls from each
  stream's declared rate, and schedules bounded reconnects via an injected
  callback. The orchestrator supplies the threads and the actual device I/O.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from syncfield.health.severity import Severity
from syncfield.types import (
    HealthEvent,
    HealthEventKind,
    StreamConnectionState,
    StreamStatus,
)

StatusCallback = Callable[[StreamStatus], None]
HealthCallback = Callable[[HealthEvent], None]
ReconnectRequest = Callable[[str], None]


def _ns(seconds: float) -> int:
    return int(seconds * 1_000_000_000)


@dataclass(frozen=True)
class ReconnectPolicy:
    """How the supervisor should retry a dropped stream.

    Immutable and side-effect-free. The default is a no-op (``max_attempts=0``)
    so opting in is explicit and the zero-policy path is a guaranteed
    behavioural no-change.

    Attributes:
        max_attempts: Maximum reconnect attempts per recovery episode. ``0``
            disables reconnect entirely (the supervisor still tracks health,
            it just never retries — a drop goes straight to ``FAILED``).
        initial_backoff_s: Delay before the first attempt.
        max_backoff_s: Upper bound on the (geometrically growing) delay.
        multiplier: Geometric growth factor between attempts.
        reconnect_on_stall: When ``True``, a sustained stall (data stopped
            without a clean capture-loop error — e.g. a BLE link that goes
            quiet) also escalates to a reconnect. When ``False`` (default),
            only an explicit capture-loop death triggers reconnect and a stall
            is reported as health but not acted on.
    """

    max_attempts: int = 0
    initial_backoff_s: float = 0.5
    max_backoff_s: float = 30.0
    multiplier: float = 2.0
    reconnect_on_stall: bool = False

    @classmethod
    def disabled(cls) -> "ReconnectPolicy":
        """A policy that never reconnects (the default)."""
        return cls(max_attempts=0)

    @property
    def enabled(self) -> bool:
        """True when at least one reconnect attempt is permitted."""
        return self.max_attempts > 0

    def backoff_s(self, attempt: int) -> float:
        """Seconds to wait before the ``attempt``-th (1-based) reconnect."""
        delay = self.initial_backoff_s * (self.multiplier ** (attempt - 1))
        return min(delay, self.max_backoff_s)


@dataclass
class _StreamRec:
    """Mutable per-stream bookkeeping owned by the supervisor."""

    sid: str
    target_hz: Optional[float] = None
    is_removable: bool = False
    supports_recording_reconnect: bool = False
    state: StreamConnectionState = StreamConnectionState.IDLE
    error: Optional[str] = None
    connected_at_ns: Optional[int] = None
    last_sample_ns: Optional[int] = None
    attempts: int = 0
    in_flight: bool = False
    next_attempt_ns: Optional[int] = None


class StreamSupervisor:
    """Per-stream health state machine and bounded reconnect scheduler.

    The supervisor is the single owner of each stream's
    :class:`~syncfield.types.StreamConnectionState`. It converts three raw,
    reliable signals — lifecycle transitions, sample arrivals, and capture-loop
    deaths — into **transition-only** status/health emissions, derives stalls
    from each stream's declared rate, and decides *when* a reconnect should be
    attempted. It performs no device I/O and owns no threads: the caller drives
    it with explicit notes and periodic :meth:`tick`, and supplies the
    ``request_reconnect`` callback that actually re-opens hardware (reporting
    the outcome back via :meth:`note_reconnect_result`).

    All state mutation happens under an internal lock; callbacks
    (``on_status`` / ``on_transition`` / ``request_reconnect``) are always
    invoked **outside** the lock so a slow or re-entrant consumer can never
    deadlock the supervisor.

    Args:
        policy: Reconnect behaviour. Default disabled — the supervisor still
            tracks and reports health, it just never retries.
        on_status: Called with a :class:`StreamStatus` on every state change.
        on_transition: Called with a :class:`HealthEvent` on notable
            transitions (stall / reconnecting / recovered / failed). Fingerprint
            is set so the event is a first-class incident downstream.
        request_reconnect: Called with a ``stream_id`` when an attempt should
            run now. The consumer performs the reconnect off-thread and calls
            :meth:`note_reconnect_result`.
        now: Monotonic-ns clock (injectable for tests).
        stall_grace_s: Silence tolerated for a streaming (rate-declared) stream
            before it is reported ``STALLED``.
        no_data_grace_s: Time after ``connect`` a rate-declared stream may
            produce nothing before it is reported ``STALLED``.
    """

    def __init__(
        self,
        policy: ReconnectPolicy = ReconnectPolicy.disabled(),
        *,
        on_status: Optional[StatusCallback] = None,
        on_transition: Optional[HealthCallback] = None,
        request_reconnect: Optional[ReconnectRequest] = None,
        now: Callable[[], int] = time.monotonic_ns,
        stall_grace_s: float = 2.0,
        no_data_grace_s: float = 30.0,
    ) -> None:
        self._policy = policy
        self._on_status = on_status
        self._on_transition = on_transition
        self._request_reconnect = request_reconnect
        self.now = now
        self._stall_grace_ns = _ns(stall_grace_s)
        self._no_data_grace_ns = _ns(no_data_grace_s)
        self._lock = threading.RLock()
        self._streams: dict[str, _StreamRec] = {}
        self._recording = False

    # -- registration ----------------------------------------------------

    def register(
        self,
        sid: str,
        *,
        target_hz: Optional[float] = None,
        is_removable: bool = False,
        supports_recording_reconnect: bool = False,
    ) -> None:
        with self._lock:
            self._streams[sid] = _StreamRec(
                sid=sid,
                target_hz=target_hz,
                is_removable=is_removable,
                supports_recording_reconnect=supports_recording_reconnect,
            )

    def unregister(self, sid: str) -> None:
        with self._lock:
            self._streams.pop(sid, None)

    def set_recording(self, is_recording: bool) -> None:
        with self._lock:
            self._recording = is_recording

    # -- lifecycle notes -------------------------------------------------

    def note_connecting(self, sid: str) -> None:
        out: list = []
        now = self.now()
        with self._lock:
            rec = self._streams.get(sid)
            if rec is not None:
                self._change(rec, StreamConnectionState.CONNECTING, None, now, out)
        self._emit(out)

    def note_connected(self, sid: str) -> None:
        out: list = []
        now = self.now()
        with self._lock:
            rec = self._streams.get(sid)
            if rec is not None:
                self._reset_connected(rec, now, out)
        self._emit(out)

    def note_connect_failed(self, sid: str, error: str) -> None:
        out: list = []
        now = self.now()
        with self._lock:
            rec = self._streams.get(sid)
            if rec is not None:
                self._change(rec, StreamConnectionState.FAILED, error, now, out)
        self._emit(out)

    def note_disconnected(self, sid: str) -> None:
        out: list = []
        now = self.now()
        with self._lock:
            rec = self._streams.get(sid)
            if rec is not None:
                self._change(
                    rec, StreamConnectionState.DISCONNECTED, None, now, out
                )
        self._emit(out)

    def note_sample(self, sid: str, at_ns: int) -> None:
        out: list = []
        with self._lock:
            rec = self._streams.get(sid)
            if rec is None:
                return
            rec.last_sample_ns = at_ns
            if rec.state in (
                StreamConnectionState.STALLED,
                StreamConnectionState.RECONNECTING,
            ):
                # Data came back on its own — recover, but keep the fresh
                # sample time so we don't immediately re-stall.
                rec.attempts = 0
                rec.in_flight = False
                rec.next_attempt_ns = None
                rec.connected_at_ns = at_ns
                self._change(rec, StreamConnectionState.CONNECTED, None, at_ns, out)
        self._emit(out)

    def note_capture_error(self, sid: str, detail: str) -> None:
        out: list = []
        now = self.now()
        with self._lock:
            rec = self._streams.get(sid)
            if rec is not None:
                if self._eligible(rec):
                    self._begin_reconnect(rec, now, detail, out)
                else:
                    self._change(
                        rec, StreamConnectionState.FAILED, detail, now, out
                    )
        self._emit(out)

    def note_reconnect_result(
        self, sid: str, ok: bool, error: Optional[str] = None
    ) -> None:
        out: list = []
        now = self.now()
        with self._lock:
            rec = self._streams.get(sid)
            if rec is None:
                self._emit(out)
                return
            rec.in_flight = False
            if ok:
                self._reset_connected(rec, now, out)
            else:
                rec.error = error or rec.error
                if rec.attempts >= self._policy.max_attempts:
                    self._change(
                        rec,
                        StreamConnectionState.FAILED,
                        rec.error or "reconnect attempts exhausted",
                        now,
                        out,
                    )
                else:
                    rec.next_attempt_ns = now + _ns(
                        self._policy.backoff_s(rec.attempts + 1)
                    )
        self._emit(out)

    # -- periodic monitor ------------------------------------------------

    def tick(self, now: Optional[int] = None) -> None:
        now = self.now() if now is None else now
        out: list = []
        reconnect_sids: list[str] = []
        with self._lock:
            for rec in list(self._streams.values()):
                self._tick_stream(rec, now, out, reconnect_sids)
        self._emit(out)
        if self._request_reconnect is not None:
            for sid in reconnect_sids:
                self._request_reconnect(sid)

    # -- queries ---------------------------------------------------------

    def status(self, sid: str) -> StreamStatus:
        with self._lock:
            return self._status_obj(self._streams[sid])

    def statuses(self) -> dict[str, StreamStatus]:
        with self._lock:
            return {
                sid: self._status_obj(rec) for sid, rec in self._streams.items()
            }

    # -- internals -------------------------------------------------------

    def _eligible(self, rec: _StreamRec) -> bool:
        if not self._policy.enabled:
            return False
        if self._recording and not rec.supports_recording_reconnect:
            return False
        return True

    def _begin_reconnect(
        self, rec: _StreamRec, now: int, error: Optional[str], out: list
    ) -> None:
        rec.attempts = 0
        rec.in_flight = False
        rec.next_attempt_ns = now + _ns(self._policy.backoff_s(1))
        self._change(rec, StreamConnectionState.RECONNECTING, error, now, out)

    def _reset_connected(self, rec: _StreamRec, now: int, out: list) -> None:
        rec.attempts = 0
        rec.in_flight = False
        rec.next_attempt_ns = None
        rec.connected_at_ns = now
        rec.last_sample_ns = None
        self._change(rec, StreamConnectionState.CONNECTED, None, now, out)

    def _tick_stream(
        self, rec: _StreamRec, now: int, out: list, reconnect_sids: list
    ) -> None:
        # Liveness: only rate-declaring streams opt into stall monitoring.
        if (
            rec.state is StreamConnectionState.CONNECTED
            and rec.target_hz is not None
        ):
            if rec.last_sample_ns is None:
                # Explicit None check: a valid connected_at_ns of 0 (clock
                # origin) is falsy and must not fall back to ``now``.
                ref = rec.connected_at_ns if rec.connected_at_ns is not None else now
                silent = now - ref
                grace = self._no_data_grace_ns
            else:
                silent = now - rec.last_sample_ns
                grace = self._stall_grace_ns
            if silent > grace:
                secs = silent / 1_000_000_000
                self._change(
                    rec,
                    StreamConnectionState.STALLED,
                    f"no data for {secs:.1f}s",
                    now,
                    out,
                )
                if self._policy.reconnect_on_stall and self._eligible(rec):
                    self._begin_reconnect(rec, now, rec.error, out)

        # Bounded reconnect scheduling.
        if (
            rec.state is StreamConnectionState.RECONNECTING
            and not rec.in_flight
            and rec.next_attempt_ns is not None
            and now >= rec.next_attempt_ns
        ):
            if rec.attempts >= self._policy.max_attempts:
                self._change(
                    rec,
                    StreamConnectionState.FAILED,
                    rec.error or "reconnect attempts exhausted",
                    now,
                    out,
                )
            else:
                rec.attempts += 1
                rec.in_flight = True
                rec.next_attempt_ns = None
                reconnect_sids.append(rec.sid)

    def _status_obj(self, rec: _StreamRec) -> StreamStatus:
        return StreamStatus(
            stream_id=rec.sid,
            state=rec.state,
            error=rec.error,
            reconnect_attempts=rec.attempts,
        )

    def _change(
        self,
        rec: _StreamRec,
        new_state: StreamConnectionState,
        error: Optional[str],
        now: int,
        out: list,
    ) -> None:
        if rec.state is new_state:
            return
        prev = rec.state
        rec.state = new_state
        rec.error = error
        out.append(("status", self._status_obj(rec)))
        kind, severity = self._health_for(prev, new_state)
        if kind is not None:
            out.append((
                "health",
                HealthEvent(
                    stream_id=rec.sid,
                    kind=kind,
                    at_ns=now,
                    detail=error,
                    severity=severity,
                    source="supervisor",
                    fingerprint=f"{rec.sid}:{new_state.value}",
                ),
            ))

    @staticmethod
    def _health_for(
        prev: StreamConnectionState, new: StreamConnectionState
    ) -> tuple[Optional[HealthEventKind], Severity]:
        S = StreamConnectionState
        if new is S.FAILED:
            return HealthEventKind.ERROR, Severity.ERROR
        if new is S.STALLED:
            return HealthEventKind.WARNING, Severity.WARNING
        if new is S.RECONNECTING:
            return HealthEventKind.RECONNECT, Severity.WARNING
        if new is S.CONNECTED:
            if prev in (S.RECONNECTING, S.STALLED):
                return HealthEventKind.RECONNECT, Severity.INFO
            return HealthEventKind.HEARTBEAT, Severity.INFO
        return None, Severity.INFO

    def _emit(self, out: list) -> None:
        for kind, payload in out:
            if kind == "status":
                if self._on_status is not None:
                    self._on_status(payload)
            elif kind == "health":
                if self._on_transition is not None:
                    self._on_transition(payload)
