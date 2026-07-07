"""End-to-end supervised reconnect through a live SessionOrchestrator.

Exercises the real supervision threads (monitor + reconnect worker): a
capture-loop death is signalled via an ERROR health event, and the
orchestrator drives ``stream.reconnect()`` with bounded backoff until the
stream recovers or the attempts are exhausted. Uses deadline polling because
the recovery happens on background threads.
"""

from __future__ import annotations

import time

from syncfield.orchestrator import SessionOrchestrator
from syncfield.supervision import ReconnectPolicy
from syncfield.testing import FakeStream
from syncfield.tone import SyncToneConfig
from syncfield.types import HealthEventKind, StreamConnectionState


class ReconnectingFakeStream(FakeStream):
    """FakeStream whose ``reconnect()`` fails a set number of times first."""

    def __init__(self, id: str, fail_reconnects: int = 0) -> None:
        super().__init__(id=id)
        self.reconnect_calls = 0
        self._fail_reconnects = fail_reconnects

    def reconnect(self) -> None:
        self.reconnect_calls += 1
        if self.reconnect_calls <= self._fail_reconnects:
            raise RuntimeError("device still gone")
        # Otherwise the device "re-opened" successfully.


def _session(tmp_path, policy):
    return SessionOrchestrator(
        host_id="rig",
        output_dir=tmp_path,
        sync_tone=SyncToneConfig.silent(),
        enable_host_audio=False,
        reconnect_policy=policy,
    )


def _wait_until(pred, timeout=4.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def test_supervised_reconnect_recovers_after_transient_failure(tmp_path):
    policy = ReconnectPolicy(max_attempts=3, initial_backoff_s=0.01)
    sess = _session(tmp_path, policy)
    stream = ReconnectingFakeStream("cam", fail_reconnects=1)
    sess.add(stream)
    sess.connect()
    assert sess.stream_status("cam").state is StreamConnectionState.CONNECTED

    # Simulate the capture loop dying.
    stream.push_health(
        HealthEventKind.ERROR, at_ns=time.monotonic_ns(), detail="usb dropped"
    )

    ok = _wait_until(
        lambda: sess.stream_status("cam").state
        is StreamConnectionState.CONNECTED
        and stream.reconnect_calls >= 2
    )
    assert ok, sess.stream_status("cam")
    assert stream.reconnect_calls == 2  # one failed attempt, then success
    assert sess.stream_status("cam").reconnect_attempts == 0  # reset on recover
    sess.disconnect()


def test_supervised_reconnect_exhausts_to_failed(tmp_path):
    policy = ReconnectPolicy(max_attempts=2, initial_backoff_s=0.01)
    sess = _session(tmp_path, policy)
    stream = ReconnectingFakeStream("cam", fail_reconnects=99)  # never recovers
    sess.add(stream)
    sess.connect()

    stream.push_health(
        HealthEventKind.ERROR, at_ns=time.monotonic_ns(), detail="dead"
    )

    ok = _wait_until(
        lambda: sess.stream_status("cam").state is StreamConnectionState.FAILED
    )
    assert ok, sess.stream_status("cam")
    assert stream.reconnect_calls == 2  # capped at max_attempts
    sess.disconnect()


def test_disabled_policy_does_not_reconnect(tmp_path):
    sess = _session(tmp_path, ReconnectPolicy.disabled())
    stream = ReconnectingFakeStream("cam")
    sess.add(stream)
    sess.connect()

    stream.push_health(
        HealthEventKind.ERROR, at_ns=time.monotonic_ns(), detail="dropped"
    )

    # With no policy the drop is terminal and nothing retries.
    ok = _wait_until(
        lambda: sess.stream_status("cam").state is StreamConnectionState.FAILED
    )
    assert ok, sess.stream_status("cam")
    assert stream.reconnect_calls == 0
    sess.disconnect()
