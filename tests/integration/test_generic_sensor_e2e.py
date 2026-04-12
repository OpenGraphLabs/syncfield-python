"""End-to-end test: SessionOrchestrator + both generic sensor helpers."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from syncfield import SessionOrchestrator, SyncToneConfig
from syncfield.adapters.polling_sensor import PollingSensorStream
from syncfield.adapters.push_sensor import PushSensorStream


def test_orchestrator_with_polling_and_push_helpers(tmp_path: Path):
    session = SessionOrchestrator(
        host_id="e2e_host",
        output_dir=tmp_path,
        sync_tone=SyncToneConfig.silent(),
    )

    # Polling helper
    poll_counter = {"n": 0}

    def read_counter():
        poll_counter["n"] += 1
        return {"i": poll_counter["n"]}

    polling = PollingSensorStream("poll_imu", read=read_counter, hz=100)
    session.add(polling)

    # Push helper
    push_stream = PushSensorStream("push_imu")
    session.add(push_stream)

    push_stop = threading.Event()
    push_count = [0]

    def push_producer():
        while not push_stop.is_set():
            push_stream.push({"v": push_count[0]})
            push_count[0] += 1
            time.sleep(0.01)

    push_thread = threading.Thread(target=push_producer, daemon=True)

    # Walk the lifecycle — start() auto-connects from IDLE
    push_thread.start()
    try:
        session.start(countdown_s=0)
        time.sleep(0.3)
        report = session.stop()
    finally:
        push_stop.set()
        push_thread.join(timeout=1.0)

    # ── Verify both streams in report ────────────────────────────
    finalizations = {f.stream_id: f for f in report.finalizations}
    assert "poll_imu" in finalizations
    assert "push_imu" in finalizations

    poll_fin = finalizations["poll_imu"]
    push_fin = finalizations["push_imu"]
    assert poll_fin.status == "completed"
    assert push_fin.status == "completed"
    # Helpers report file_path=None (orchestrator owns the file)
    assert poll_fin.file_path is None
    assert push_fin.file_path is None

    # ── Verify JSONL files (written by orchestrator) ─────────────
    poll_path = session.output_dir / "poll_imu.jsonl"
    push_path = session.output_dir / "push_imu.jsonl"
    assert poll_path.exists(), "orchestrator should create poll_imu.jsonl"
    assert push_path.exists(), "orchestrator should create push_imu.jsonl"

    poll_lines = poll_path.read_text().strip().split("\n")
    push_lines = push_path.read_text().strip().split("\n")
    assert len(poll_lines) > 0
    assert len(push_lines) > 0

    # JSONL records match the SensorSample schema
    first_poll = json.loads(poll_lines[0])
    assert "frame_number" in first_poll
    assert "capture_ns" in first_poll
    assert "channels" in first_poll
    assert first_poll["clock_source"] == "host_monotonic"

    # ── Verify manifest ──────────────────────────────────────────
    manifest_path = session.output_dir / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert "poll_imu" in manifest["streams"]
    assert "push_imu" in manifest["streams"]
