"""The per-stream health state machine (``StreamSupervisor``).

The supervisor is deliberately thread-free and I/O-free: every behaviour is
driven by explicit lifecycle notes plus ``tick(now_ns)`` against a fake clock,
so stall detection and reconnect backoff are fully deterministic. The
orchestrator later supplies the real thread and the device I/O; here we only
prove the decision logic.
"""

from __future__ import annotations

from syncfield.supervision import ReconnectPolicy, StreamSupervisor
from syncfield.types import HealthEventKind, StreamConnectionState


def _s(seconds: float) -> int:
    return int(seconds * 1_000_000_000)


class FakeClock:
    """Monotonic-ns clock the test advances by hand."""

    def __init__(self, start_ns: int = 0) -> None:
        self.t = start_ns

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += _s(seconds)


class Recorder:
    """Collects supervisor outputs for assertions."""

    def __init__(self) -> None:
        self.statuses: list = []
        self.health: list = []
        self.reconnect_requests: list[str] = []

    def on_status(self, status) -> None:
        self.statuses.append(status)

    def on_transition(self, event) -> None:
        self.health.append(event)

    def request_reconnect(self, sid: str) -> None:
        self.reconnect_requests.append(sid)

    def states_for(self, sid: str) -> list:
        return [s.state for s in self.statuses if s.stream_id == sid]


def _supervisor(policy=None, clock=None, rec=None, **kwargs):
    rec = rec or Recorder()
    clock = clock or FakeClock()
    sup = StreamSupervisor(
        policy or ReconnectPolicy.disabled(),
        on_status=rec.on_status,
        on_transition=rec.on_transition,
        request_reconnect=rec.request_reconnect,
        now=clock,
        **kwargs,
    )
    return sup, rec, clock


class TestLifecycleTransitions:
    def test_register_starts_idle(self):
        sup, rec, _ = _supervisor()
        sup.register("cam")
        assert sup.status("cam").state is StreamConnectionState.IDLE

    def test_connecting_then_connected(self):
        sup, rec, _ = _supervisor()
        sup.register("cam")
        sup.note_connecting("cam")
        sup.note_connected("cam")
        assert sup.status("cam").state is StreamConnectionState.CONNECTED
        assert rec.states_for("cam")[-2:] == [
            StreamConnectionState.CONNECTING,
            StreamConnectionState.CONNECTED,
        ]

    def test_connect_failure_is_terminal_failed_with_error(self):
        sup, rec, _ = _supervisor()
        sup.register("cam")
        sup.note_connecting("cam")
        sup.note_connect_failed("cam", "device busy")
        status = sup.status("cam")
        assert status.state is StreamConnectionState.FAILED
        assert status.error == "device busy"

    def test_clean_disconnect(self):
        sup, rec, _ = _supervisor()
        sup.register("cam")
        sup.note_connected("cam")
        sup.note_disconnected("cam")
        assert sup.status("cam").state is StreamConnectionState.DISCONNECTED

    def test_status_change_only_emits_on_transition(self):
        # Re-notifying the same state must not spam callbacks.
        sup, rec, _ = _supervisor()
        sup.register("cam")
        sup.note_connected("cam")
        n = len(rec.statuses)
        sup.note_connected("cam")
        assert len(rec.statuses) == n


class TestStallDetection:
    def test_connected_stream_goes_stalled_after_silence(self):
        sup, rec, clock = _supervisor(stall_grace_s=2.0)
        sup.register("oglo", target_hz=100.0)
        sup.note_connected("oglo")
        sup.note_sample("oglo", clock())
        clock.advance(3.0)  # exceeds the 2s stall grace
        sup.tick()
        assert sup.status("oglo").state is StreamConnectionState.STALLED

    def test_stall_emits_once_not_every_tick(self):
        sup, rec, clock = _supervisor(stall_grace_s=2.0)
        sup.register("oglo", target_hz=100.0)
        sup.note_connected("oglo")
        sup.note_sample("oglo", clock())
        clock.advance(3.0)
        sup.tick()
        stalled_emits = [s for s in rec.statuses
                         if s.stream_id == "oglo"
                         and s.state is StreamConnectionState.STALLED]
        clock.advance(1.0)
        sup.tick()
        stalled_after = [s for s in rec.statuses
                         if s.stream_id == "oglo"
                         and s.state is StreamConnectionState.STALLED]
        assert len(stalled_emits) == 1
        assert len(stalled_after) == 1

    def test_sample_recovers_from_stall(self):
        sup, rec, clock = _supervisor(stall_grace_s=2.0)
        sup.register("oglo", target_hz=100.0)
        sup.note_connected("oglo")
        sup.note_sample("oglo", clock())
        clock.advance(3.0)
        sup.tick()
        assert sup.status("oglo").state is StreamConnectionState.STALLED
        clock.advance(0.1)
        sup.note_sample("oglo", clock())
        assert sup.status("oglo").state is StreamConnectionState.CONNECTED

    def test_connected_but_never_streamed_stalls_after_no_data_grace(self):
        sup, rec, clock = _supervisor(no_data_grace_s=30.0)
        sup.register("cam", target_hz=30.0)
        sup.note_connected("cam")  # no sample ever
        clock.advance(31.0)
        sup.tick()
        assert sup.status("cam").state is StreamConnectionState.STALLED


class TestReconnectDisabled:
    def test_capture_error_without_policy_goes_failed(self):
        sup, rec, _ = _supervisor(policy=ReconnectPolicy.disabled())
        sup.register("cam", is_removable=True)
        sup.note_connected("cam")
        sup.note_capture_error("cam", "usb unplugged")
        status = sup.status("cam")
        assert status.state is StreamConnectionState.FAILED
        assert status.error == "usb unplugged"

    def test_disabled_policy_never_requests_reconnect(self):
        sup, rec, clock = _supervisor(policy=ReconnectPolicy.disabled())
        sup.register("cam", is_removable=True)
        sup.note_connected("cam")
        sup.note_capture_error("cam", "gone")
        clock.advance(100.0)
        sup.tick()
        assert rec.reconnect_requests == []


class TestReconnectEnabled:
    def _policy(self):
        return ReconnectPolicy(
            max_attempts=3, initial_backoff_s=1.0, multiplier=2.0
        )

    def test_capture_error_enters_reconnecting(self):
        sup, rec, _ = _supervisor(policy=self._policy())
        sup.register("cam", is_removable=True)
        sup.note_connected("cam")
        sup.note_capture_error("cam", "dropped")
        assert sup.status("cam").state is StreamConnectionState.RECONNECTING

    def test_reconnecting_emits_reconnect_health_kind(self):
        sup, rec, _ = _supervisor(policy=self._policy())
        sup.register("cam", is_removable=True)
        sup.note_connected("cam")
        sup.note_capture_error("cam", "dropped")
        kinds = [e.kind for e in rec.health if e.stream_id == "cam"]
        assert HealthEventKind.RECONNECT in kinds

    def test_reconnect_request_fires_after_backoff(self):
        sup, rec, clock = _supervisor(policy=self._policy())
        sup.register("cam", is_removable=True)
        sup.note_connected("cam")
        sup.note_capture_error("cam", "dropped")
        # No attempt before the 1s initial backoff elapses.
        sup.tick()
        assert rec.reconnect_requests == []
        clock.advance(1.0)
        sup.tick()
        assert rec.reconnect_requests == ["cam"]

    def test_successful_reconnect_returns_to_connected_and_resets_attempts(self):
        sup, rec, clock = _supervisor(policy=self._policy())
        sup.register("cam", is_removable=True)
        sup.note_connected("cam")
        sup.note_capture_error("cam", "dropped")
        clock.advance(1.0)
        sup.tick()
        sup.note_reconnect_result("cam", ok=True)
        status = sup.status("cam")
        assert status.state is StreamConnectionState.CONNECTED
        assert status.reconnect_attempts == 0

    def test_attempts_exhaust_to_failed(self):
        sup, rec, clock = _supervisor(policy=self._policy())
        sup.register("cam", is_removable=True)
        sup.note_connected("cam")
        sup.note_capture_error("cam", "dropped")
        # Drive 3 failed attempts through the backoff schedule.
        for _ in range(3):
            clock.advance(100.0)  # always past any backoff cap
            sup.tick()
            sup.note_reconnect_result("cam", ok=False, error="still gone")
        assert len(rec.reconnect_requests) == 3
        status = sup.status("cam")
        assert status.state is StreamConnectionState.FAILED
        assert status.reconnect_attempts == 3

    def test_failed_terminal_emits_error_health(self):
        sup, rec, clock = _supervisor(
            policy=ReconnectPolicy(max_attempts=1, initial_backoff_s=0.0)
        )
        sup.register("cam", is_removable=True)
        sup.note_connected("cam")
        sup.note_capture_error("cam", "dropped")
        clock.advance(1.0)
        sup.tick()
        sup.note_reconnect_result("cam", ok=False, error="dead")
        error_kinds = [e.kind for e in rec.health
                       if e.stream_id == "cam"
                       and e.kind is HealthEventKind.ERROR]
        assert error_kinds
        assert sup.status("cam").state is StreamConnectionState.FAILED


class TestRecordingPhaseGate:
    def _policy(self):
        return ReconnectPolicy(max_attempts=3, initial_backoff_s=0.0)

    def test_recording_drop_not_reconnected_without_capability(self):
        sup, rec, _ = _supervisor(policy=self._policy())
        sup.register("cam", is_removable=True, supports_recording_reconnect=False)
        sup.note_connected("cam")
        sup.set_recording(True)
        sup.note_capture_error("cam", "dropped mid-record")
        # Video adapters can't safely resume -> straight to FAILED, app decides.
        assert sup.status("cam").state is StreamConnectionState.FAILED

    def test_recording_drop_reconnected_with_capability(self):
        sup, rec, _ = _supervisor(policy=self._policy())
        sup.register("oglo", is_removable=True, supports_recording_reconnect=True)
        sup.note_connected("oglo")
        sup.set_recording(True)
        sup.note_capture_error("oglo", "ble dropped mid-record")
        assert sup.status("oglo").state is StreamConnectionState.RECONNECTING

    def test_preview_drop_reconnected_regardless_of_capability(self):
        sup, rec, _ = _supervisor(policy=self._policy())
        sup.register("cam", is_removable=True, supports_recording_reconnect=False)
        sup.note_connected("cam")  # not recording
        sup.note_capture_error("cam", "dropped in preview")
        assert sup.status("cam").state is StreamConnectionState.RECONNECTING


class TestStallEscalation:
    def test_stall_escalates_to_reconnect_when_policy_opts_in(self):
        policy = ReconnectPolicy(
            max_attempts=3, initial_backoff_s=0.0, reconnect_on_stall=True
        )
        sup, rec, clock = _supervisor(policy=policy, stall_grace_s=2.0)
        sup.register("oglo", target_hz=100.0, is_removable=True)
        sup.note_connected("oglo")
        sup.note_sample("oglo", clock())
        clock.advance(3.0)
        sup.tick()  # -> STALLED, and because reconnect_on_stall, escalates
        assert sup.status("oglo").state is StreamConnectionState.RECONNECTING

    def test_stall_does_not_escalate_by_default(self):
        policy = ReconnectPolicy(max_attempts=3, initial_backoff_s=0.0)
        sup, rec, clock = _supervisor(policy=policy, stall_grace_s=2.0)
        sup.register("oglo", target_hz=100.0, is_removable=True)
        sup.note_connected("oglo")
        sup.note_sample("oglo", clock())
        clock.advance(3.0)
        sup.tick()
        assert sup.status("oglo").state is StreamConnectionState.STALLED


class TestStatusesSnapshot:
    def test_statuses_returns_all_registered(self):
        sup, rec, _ = _supervisor()
        sup.register("a")
        sup.register("b")
        sup.note_connected("a")
        snap = sup.statuses()
        assert set(snap) == {"a", "b"}
        assert snap["a"].state is StreamConnectionState.CONNECTED
        assert snap["b"].state is StreamConnectionState.IDLE
