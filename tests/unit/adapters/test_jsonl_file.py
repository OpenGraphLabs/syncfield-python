"""Unit tests for JSONLFileStream adapter."""

from __future__ import annotations

import json

import pytest

from syncfield.adapters.jsonl_file import JSONLFileStream
from syncfield.clock import SessionClock
from syncfield.types import SyncPoint


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


def test_capabilities():
    stream = JSONLFileStream("log", file_path="/tmp/log.jsonl")
    assert stream.capabilities.produces_file is True
    assert stream.capabilities.provides_audio_track is False
    assert stream.kind == "sensor"


def test_lifecycle_reports_known_path_and_counts_lines(tmp_path):
    log_path = tmp_path / "custom.jsonl"
    log_path.write_text(
        json.dumps({"frame_number": 0, "capture_ns": 1}) + "\n"
        + json.dumps({"frame_number": 1, "capture_ns": 2}) + "\n"
    )
    stream = JSONLFileStream("custom", file_path=log_path)
    stream.prepare()
    stream.start(_clock())
    report = stream.stop()
    assert report.status == "completed"
    assert report.file_path == log_path
    assert report.frame_count == 2


def test_missing_file_returns_partial_status(tmp_path):
    stream = JSONLFileStream("missing", file_path=tmp_path / "nope.jsonl")
    stream.prepare()
    stream.start(_clock())
    report = stream.stop()
    assert report.status == "partial"
    assert report.frame_count == 0
    assert report.file_path is None


def test_start_without_prepare_raises():
    stream = JSONLFileStream("x", file_path="/tmp/x.jsonl")
    with pytest.raises(RuntimeError, match="prepare"):
        stream.start(_clock())


def test_empty_file_is_completed_with_zero_frames(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    stream = JSONLFileStream("empty", file_path=p)
    stream.prepare()
    stream.start(_clock())
    report = stream.stop()
    assert report.status == "completed"
    assert report.frame_count == 0
    assert report.file_path == p
