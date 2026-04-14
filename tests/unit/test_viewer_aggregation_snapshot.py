from unittest.mock import MagicMock

import pytest

from syncfield.adapters.insta360_go3s.aggregation.types import (
    AggregationProgress,
    AggregationState,
)
from syncfield.viewer.server import snapshot_to_dict


def test_snapshot_includes_aggregation_section_empty_by_default():
    snapshot = MagicMock()
    snapshot.aggregation = None
    d = snapshot_to_dict(snapshot)
    assert "aggregation" in d
    assert d["aggregation"]["active_job"] is None
    assert d["aggregation"]["queue_length"] == 0
    assert d["aggregation"]["recent_jobs"] == []


def test_snapshot_serializes_active_job():
    progress = AggregationProgress(
        job_id="agg_x",
        episode_id="ep_x",
        state=AggregationState.RUNNING,
        cameras_total=2,
        cameras_done=1,
        current_stream_id="overhead",
        current_bytes=5_000_000,
        current_total_bytes=10_000_000,
    )
    snapshot = MagicMock()
    snapshot.aggregation = MagicMock()
    snapshot.aggregation.active_job = progress
    snapshot.aggregation.queue_length = 1
    snapshot.aggregation.recent_jobs = [progress]
    d = snapshot_to_dict(snapshot)
    assert d["aggregation"]["active_job"]["state"] == "running"
    assert d["aggregation"]["active_job"]["current_bytes"] == 5_000_000
    assert d["aggregation"]["queue_length"] == 1
    assert len(d["aggregation"]["recent_jobs"]) == 1
