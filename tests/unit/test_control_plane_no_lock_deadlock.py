"""Regression: Phase 8 surfaced a cross-thread deadlock where the main
thread held self._lock through _maybe_wait_for_leader while the
control-plane HTTP handler tried to acquire the same lock to read
has_audio_stream — POSTs from the leader timed out.

Verify the adapter properties are now lock-free."""

import threading
from pathlib import Path

import syncfield as sf
from tests.unit.conftest import FakeStream


def test_has_audio_stream_does_not_acquire_orchestrator_lock(tmp_path: Path) -> None:
    """If has_audio_stream still acquired the lock, this test would
    hang forever — main thread holds the lock throughout."""
    session = sf.SessionOrchestrator(
        host_id="mac_a",
        output_dir=tmp_path,
        role=sf.LeaderRole(session_id="t-1", control_plane_port=0),
    )
    session.add(FakeStream("cam"))
    mic = FakeStream("mic")
    mic.kind = "audio"
    session.add(mic)
    session._start_control_plane_only_for_tests()

    try:
        adapter = session._build_control_plane_adapter()

        # Hold the orchestrator lock from this thread.
        with session._lock:
            # If has_audio_stream still acquired the lock, the next
            # call would re-enter (RLock allows this on the SAME
            # thread). To simulate cross-thread access, run from a
            # worker thread and assert it returns within a short window.
            result = {}
            done = threading.Event()

            def worker():
                # This must NOT acquire session._lock.
                result["has_audio"] = adapter.has_audio_stream
                done.set()

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            assert done.wait(timeout=2.0), (
                "adapter.has_audio_stream blocked on orchestrator lock — "
                "deadlock regression"
            )
            assert result["has_audio"] is True
    finally:
        session._stop_control_plane_only_for_tests()


def test_snapshot_stream_metrics_does_not_acquire_lock(tmp_path: Path) -> None:
    session = sf.SessionOrchestrator(
        host_id="mac_a",
        output_dir=tmp_path,
        role=sf.LeaderRole(session_id="t-2", control_plane_port=0),
    )
    session.add(FakeStream("cam"))
    mic = FakeStream("mic")
    mic.kind = "audio"
    session.add(mic)
    session._start_control_plane_only_for_tests()

    try:
        adapter = session._build_control_plane_adapter()
        with session._lock:
            done = threading.Event()
            result = {}

            def worker():
                result["metrics"] = adapter.snapshot_stream_metrics()
                done.set()

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            assert done.wait(timeout=2.0), (
                "adapter.snapshot_stream_metrics blocked on lock — "
                "deadlock regression"
            )
            assert len(result["metrics"]) == 2
    finally:
        session._stop_control_plane_only_for_tests()
