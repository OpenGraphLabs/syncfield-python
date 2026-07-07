"""Public per-stream status API on the orchestrator.

Replaces reaching into the private ``_stream_states`` / ``_stream_errors``
dicts. Driven by the same lifecycle transitions, so the snapshot always
matches what the orchestrator actually did to each stream.
"""

from __future__ import annotations

from syncfield.orchestrator import SessionOrchestrator
from syncfield.testing import FakeStream
from syncfield.tone import SyncToneConfig
from syncfield.types import StreamConnectionState


def _session(tmp_path):
    return SessionOrchestrator(
        host_id="rig_01",
        output_dir=tmp_path,
        sync_tone=SyncToneConfig.silent(),
        enable_host_audio=False,
    )


class TestStreamStatus:
    def test_reflects_connected(self, tmp_path):
        sess = _session(tmp_path)
        sess.add(FakeStream("cam"))
        sess.connect()
        assert sess.stream_status("cam").state is StreamConnectionState.CONNECTED
        sess.disconnect()

    def test_reflects_connect_failure(self, tmp_path):
        sess = _session(tmp_path)
        sess.add(FakeStream("good"))
        sess.add(FakeStream("bad", fail_on_start=True))
        sess.connect()
        good = sess.stream_status("good")
        bad = sess.stream_status("bad")
        assert good.state is StreamConnectionState.CONNECTED
        assert bad.state is StreamConnectionState.FAILED
        assert bad.error is not None
        sess.disconnect()

    def test_statuses_returns_all_streams(self, tmp_path):
        sess = _session(tmp_path)
        sess.add(FakeStream("a"))
        sess.add(FakeStream("b"))
        snap = sess.stream_statuses()
        assert set(snap) == {"a", "b"}
        assert snap["a"].state is StreamConnectionState.IDLE

    def test_disconnected_after_disconnect(self, tmp_path):
        sess = _session(tmp_path)
        sess.add(FakeStream("cam"))
        sess.connect()
        sess.disconnect()
        assert (
            sess.stream_status("cam").state
            is StreamConnectionState.DISCONNECTED
        )


class TestOnStreamStatusCallback:
    def test_callback_fires_on_connect(self, tmp_path):
        sess = _session(tmp_path)
        seen = []
        sess.on_stream_status(seen.append)
        sess.add(FakeStream("cam"))
        sess.connect()
        states = [s.state for s in seen if s.stream_id == "cam"]
        assert StreamConnectionState.CONNECTED in states
        sess.disconnect()
