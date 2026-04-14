from pathlib import Path
from syncfield.types import FinalizationReport


def test_finalization_report_accepts_pending_aggregation_status():
    report = FinalizationReport(
        stream_id="overhead",
        status="pending_aggregation",
        frame_count=0,
        file_path=None,
        first_sample_at_ns=None,
        last_sample_at_ns=None,
        health_events=[],
        error=None,
    )
    assert report.status == "pending_aggregation"
