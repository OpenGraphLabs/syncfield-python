from unittest.mock import MagicMock

import pytest

from syncfield.adapters.insta360_go3s.aggregation.types import (
    AggregationProgress,
    AggregationState,
)
from syncfield.viewer.server import snapshot_to_dict


def _make_snapshot_mock(**kwargs):
    snapshot = MagicMock()
    snapshot.streams = {}
    snapshot.active_incidents = []
    snapshot.resolved_incidents = []
    for k, v in kwargs.items():
        setattr(snapshot, k, v)
    return snapshot


def test_snapshot_includes_aggregation_section_empty_by_default():
    snapshot = _make_snapshot_mock(aggregation=None)
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
    agg_mock = MagicMock()
    agg_mock.active_job = progress
    agg_mock.queue_length = 1
    agg_mock.recent_jobs = [progress]
    snapshot = _make_snapshot_mock(aggregation=agg_mock)
    d = snapshot_to_dict(snapshot)
    assert d["aggregation"]["active_job"]["state"] == "running"
    assert d["aggregation"]["active_job"]["current_bytes"] == 5_000_000
    assert d["aggregation"]["queue_length"] == 1
    assert len(d["aggregation"]["recent_jobs"]) == 1
