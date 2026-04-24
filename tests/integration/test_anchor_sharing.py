"""All streams in one session observe the SAME armed_host_ns.

This is the end-to-end validation that intra-host sync metadata is
wired all the way through SessionOrchestrator -> SessionClock ->
adapters -> FinalizationReport -> manifest.json.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from syncfield.adapters.polling_sensor import PollingSensorStream
from syncfield.orchestrator import SessionOrchestrator
from syncfield.tone import SyncToneConfig


def test_all_streams_share_armed_host_ns(tmp_path: Path) -> None:
    """3 polling sensors, run a short session, verify all see same armed_ns."""
    sess = SessionOrchestrator(
        host_id="h",
        output_dir=tmp_path,
        sync_tone=SyncToneConfig.silent(),  # disable audio chirp
    )
    sess.add(PollingSensorStream("a", read=lambda: {"x": 1.0}, hz=100))
    sess.add(PollingSensorStream("b", read=lambda: {"y": 2.0}, hz=100))
    sess.add(PollingSensorStream("c", read=lambda: {"z": 3.0}, hz=100))
    sess.connect()
    sess.start(countdown_s=0)  # skip default countdown for test speed
    time.sleep(0.1)
    sess.stop()

    manifest_path = next(tmp_path.rglob("manifest.json"), None)
    assert manifest_path is not None, "manifest.json not written"
    manifest = json.loads(manifest_path.read_text())
    streams = manifest.get("streams", {})

    anchors = [
        entry["recording_anchor"]
        for entry in streams.values()
        if entry.get("recording_anchor") is not None
    ]
    assert len(anchors) == 3, f"expected 3 anchors, got {len(anchors)}"

    armed_values = {a["armed_host_ns"] for a in anchors}
    assert len(armed_values) == 1, (
        f"All streams must share the same armed_host_ns; got {armed_values}"
    )

    for a in anchors:
        assert a["first_frame_host_ns"] >= a["armed_host_ns"]
        assert a["first_frame_latency_ns"] >= 0
