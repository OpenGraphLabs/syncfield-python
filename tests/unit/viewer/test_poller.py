"""Integration-ish tests for the poller against a real SessionOrchestrator.

We can't reasonably unit-test the full DPG app in CI, but the poller is a
pure Python object that just reads session attributes, so we can exercise
it end-to-end with FakeStreams and verify that snapshots match reality.
"""

from __future__ import annotations

import time

import syncfield as sf
from syncfield.testing import FakeStream
from syncfield.viewer.poller import SessionPoller


def _make_session(tmp_path):
    session = sf.SessionOrchestrator(
        host_id="test_rig",
        output_dir=tmp_path,
        sync_tone=sf.SyncToneConfig.silent(),
    )
    session.add(FakeStream("cam", provides_audio_track=True))
    session.add(FakeStream("imu"))
    return session


class TestSnapshotBuilding:
    def test_idle_snapshot_has_zero_elapsed(self, tmp_path):
        session = _make_session(tmp_path)
        poller = SessionPoller(session, interval_s=0.01)
        snap = poller._build_snapshot()

        assert snap.host_id == "test_rig"
        assert snap.state == "idle"
        assert snap.elapsed_s == 0.0
        assert snap.sync_point_monotonic_ns is None
        assert snap.chirp_start_ns is None
        assert set(snap.streams.keys()) == {"cam", "imu"}

    def test_recording_snapshot_populates_sync_point(self, tmp_path):
        session = _make_session(tmp_path)
        poller = SessionPoller(session, interval_s=0.01)

        session.start()
        try:
            snap = poller._build_snapshot()
            assert snap.state == "recording"
            assert snap.sync_point_monotonic_ns is not None
            assert snap.sync_point_wall_clock_ns is not None
        finally:
            session.stop()

    def test_stream_capabilities_propagate(self, tmp_path):
        session = _make_session(tmp_path)
        poller = SessionPoller(session, interval_s=0.01)
        snap = poller._build_snapshot()

        assert snap.streams["cam"].provides_audio_track is True
        assert snap.streams["imu"].provides_audio_track is False

    def test_push_sample_shows_up_in_next_snapshot(self, tmp_path):
        session = _make_session(tmp_path)
        poller = SessionPoller(session, interval_s=0.01)

        # Register callbacks (normally done by start(), but we can call
        # directly for tests).
        poller._register_callbacks()

        session.start()
        try:
            imu = session._streams["imu"]  # type: ignore[attr-defined]
            for i in range(10):
                imu.push_sample(frame_number=i, capture_ns=time.monotonic_ns())
            snap = poller._build_snapshot()
            assert snap.streams["imu"].frame_count == 10
            assert snap.streams["imu"].last_sample_at_ns is not None
        finally:
            session.stop()

    def test_snapshot_has_incident_lists(self, tmp_path):
        """Verify that SessionSnapshot carries active_incidents / resolved_incidents
        (replacing the retired health_log / health_count fields from Task 20)."""
        session = _make_session(tmp_path)
        poller = SessionPoller(session, interval_s=0.01)
        snap = poller._build_snapshot()
        # No incidents yet — both lists should be present and empty.
        assert hasattr(snap, "active_incidents")
        assert hasattr(snap, "resolved_incidents")
        assert snap.active_incidents == []
        assert snap.resolved_incidents == []

    def test_start_stop_and_get_snapshot_thread(self, tmp_path):
        """End-to-end smoke test of the polling thread."""
        session = _make_session(tmp_path)
        poller = SessionPoller(session, interval_s=0.01)
        poller.start()
        try:
            # Give the poller a few ticks to populate snapshot
            time.sleep(0.05)
            snap = poller.get_snapshot()
            assert snap is not None
            assert snap.host_id == "test_rig"
        finally:
            poller.stop()

    def test_poller_snapshot_includes_connection_state(self, tmp_path):
        """Verify that StreamSnapshot carries connection_state and connection_error
        from the orchestrator's per-stream tracking."""
        session = sf.SessionOrchestrator(
            host_id="h",
            output_dir=tmp_path,
            sync_tone=sf.SyncToneConfig.silent(),
        )
        session.add(FakeStream("good"))
        session.add(FakeStream("bad", fail_on_start=True))

        poller = SessionPoller(session)
        session.connect()

        snap = poller._build_snapshot()
        assert snap.streams["good"].connection_state == "connected"
        assert snap.streams["good"].connection_error is None
        assert snap.streams["bad"].connection_state == "failed"
        assert snap.streams["bad"].connection_error is not None
