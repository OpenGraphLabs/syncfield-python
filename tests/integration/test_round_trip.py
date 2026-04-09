"""Round-trip integration test: full session → on-disk files → schema check.

Validates that a SessionOrchestrator-driven session produces output files
whose shape matches what the existing SyncField sync core ingests
(``manifest.json``, ``sync_point.json``, and per-stream JSONL). This test
is deliberately high-level — no mocks of our own types — so that it
exercises the whole stack end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path

from syncfield import SessionOrchestrator, SyncToneConfig
from syncfield.testing import FakeStream


def _two_stream_session(tmp_path: Path) -> SessionOrchestrator:
    session = SessionOrchestrator(
        host_id="rig_01",
        output_dir=tmp_path,
        sync_tone=SyncToneConfig.silent(),
    )
    session.add(FakeStream("cam_a", provides_audio_track=True))
    session.add(FakeStream("imu_a"))
    return session


def test_full_session_produces_valid_core_artifacts(tmp_path: Path):
    session = _two_stream_session(tmp_path)

    session.start()
    cam = session._streams["cam_a"]  # type: ignore[attr-defined]
    imu = session._streams["imu_a"]  # type: ignore[attr-defined]
    assert isinstance(cam, FakeStream)
    assert isinstance(imu, FakeStream)
    for i in range(10):
        cam.push_sample(frame_number=i, capture_ns=1_000_000 * (i + 1))
    for i in range(5):
        imu.push_sample(frame_number=i, capture_ns=2_000_000 * (i + 1))

    report = session.stop()

    # --- sync_point.json --------------------------------------------------
    sp = json.loads((tmp_path / "sync_point.json").read_text())
    assert sp["host_id"] == "rig_01"
    assert isinstance(sp["monotonic_ns"], int)
    assert isinstance(sp["wall_clock_ns"], int)
    assert "sdk_version" in sp
    # Silent tone → no chirp fields
    assert "chirp_start_ns" not in sp

    # --- manifest.json ----------------------------------------------------
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["host_id"] == "rig_01"
    assert "cam_a" in manifest["streams"]
    assert "imu_a" in manifest["streams"]
    cam_entry = manifest["streams"]["cam_a"]
    assert cam_entry["capabilities"]["provides_audio_track"] is True
    assert cam_entry["status"] == "completed"
    assert cam_entry["frame_count"] == 10

    # --- session_log.jsonl ------------------------------------------------
    log_lines = [
        json.loads(line)
        for line in (tmp_path / "session_log.jsonl")
        .read_text()
        .strip()
        .split("\n")
    ]
    transitions = [l for l in log_lines if l["kind"] == "state_transition"]
    edges = [(t["from"], t["to"]) for t in transitions]
    assert ("idle", "preparing") in edges
    assert ("preparing", "recording") in edges
    assert ("recording", "stopping") in edges
    assert ("stopping", "stopped") in edges

    # --- SessionReport ----------------------------------------------------
    assert report.host_id == "rig_01"
    assert len(report.finalizations) == 2
    by_id = {f.stream_id: f for f in report.finalizations}
    assert by_id["cam_a"].frame_count == 10
    assert by_id["imu_a"].frame_count == 5


def test_silent_session_omits_chirp_fields(tmp_path: Path):
    session = SessionOrchestrator(
        host_id="rig_01",
        output_dir=tmp_path,
        sync_tone=SyncToneConfig.silent(),
    )
    session.add(FakeStream("cam", provides_audio_track=True))
    session.start()
    session.stop()
    sp = json.loads((tmp_path / "sync_point.json").read_text())
    assert "chirp_start_ns" not in sp
    assert "chirp_stop_ns" not in sp
    assert "chirp_spec" not in sp


def test_no_audio_stream_single_host_session_works_without_chirp(tmp_path: Path):
    session = SessionOrchestrator(
        host_id="rig_solo",
        output_dir=tmp_path,
        sync_tone=SyncToneConfig.default(),  # enabled by default
    )
    # No audio-capable stream → chirp is skipped silently with an INFO log
    session.add(FakeStream("imu_only"))
    session.start()
    session.stop()
    sp = json.loads((tmp_path / "sync_point.json").read_text())
    assert "chirp_start_ns" not in sp
    # Session still completes cleanly
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["streams"]["imu_only"]["status"] == "completed"
