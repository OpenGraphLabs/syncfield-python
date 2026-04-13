"""Unit tests for FinalizationReport jitter fields."""

from __future__ import annotations

from syncfield.types import FinalizationReport


def test_finalization_report_accepts_jitter_fields() -> None:
    report = FinalizationReport(
        stream_id="uvc0",
        status="completed",
        frame_count=100,
        file_path=None,
        first_sample_at_ns=0,
        last_sample_at_ns=3_000_000_000,
        health_events=[],
        error=None,
        jitter_p95_ns=2_500_000,
        jitter_p99_ns=4_000_000,
    )
    assert report.jitter_p95_ns == 2_500_000
    assert report.jitter_p99_ns == 4_000_000


def test_finalization_report_jitter_defaults_none() -> None:
    report = FinalizationReport(
        stream_id="ble0",
        status="completed",
        frame_count=0,
        file_path=None,
        first_sample_at_ns=None,
        last_sample_at_ns=None,
        health_events=[],
        error=None,
    )
    assert report.jitter_p95_ns is None
    assert report.jitter_p99_ns is None
