from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_aggregate_episode_dispatches_to_orchestrator():
    from syncfield.viewer.server import handle_control_command

    orch = MagicMock()
    result = handle_control_command(orch, {"command": "aggregate_episode", "episode_id": "ep_x"})
    orch.aggregate_episode.assert_called_once_with("ep_x")
    assert result["ok"] is True


def test_retry_aggregation_dispatches_to_queue():
    from syncfield.viewer.server import handle_control_command

    orch = MagicMock()
    result = handle_control_command(orch, {"command": "retry_aggregation", "job_id": "agg_x"})
    orch.retry_aggregation.assert_called_once_with("agg_x")
    assert result["ok"] is True


def test_cancel_aggregation_dispatches_to_queue():
    from syncfield.viewer.server import handle_control_command

    orch = MagicMock()
    # cancel raises NotImplementedError on the orch side; the dispatcher should
    # surface that as a structured error rather than letting it propagate.
    orch.cancel_aggregation.side_effect = NotImplementedError("v2 only")
    result = handle_control_command(orch, {"command": "cancel_aggregation", "job_id": "agg_x"})
    assert result["ok"] is False
    assert "v2" in result["error"] or "NotImpl" in result["error"]


def test_unknown_command_returns_error():
    from syncfield.viewer.server import handle_control_command

    orch = MagicMock()
    result = handle_control_command(orch, {"command": "no_such_command"})
    assert result["ok"] is False
    assert "unknown" in result["error"].lower()


def test_orchestrator_aggregate_episode_finds_pending_job():
    """Orchestrator.aggregate_episode locates the right Go3SStream by episode_id."""
    from unittest.mock import patch

    with patch("syncfield.adapters.insta360_go3s.stream.Go3SBLECamera"):
        from syncfield.adapters.insta360_go3s import Go3SStream
        from syncfield.adapters.insta360_go3s.aggregation.queue import AggregationQueue
        from syncfield.adapters.insta360_go3s.aggregation.types import (
            AggregationCameraSpec, AggregationJob, AggregationState,
        )
        from syncfield.orchestrator import SessionOrchestrator

        # Build orchestrator + stream; manually set pending_aggregation_job
        from pathlib import Path
        import tempfile
        tmpdir = Path(tempfile.mkdtemp())
        session = SessionOrchestrator(host_id="mac", output_dir=tmpdir)
        s = Go3SStream(
            stream_id="overhead",
            ble_address="AA:BB:CC:DD:EE:FF",
            output_dir=tmpdir / "ep_test",
            aggregation_policy="on_demand",
        )
        session.add(s)
        s.pending_aggregation_job = AggregationJob(
            job_id="agg_test",
            episode_id="ep_test",
            episode_dir=tmpdir / "ep_test",
            cameras=[
                AggregationCameraSpec(
                    stream_id="overhead",
                    ble_address="AA:BB:CC:DD:EE:FF",
                    wifi_ssid="Go3S-X.OSC",
                    wifi_password="88888888",
                    sd_path="/DCIM/Camera01/X.mp4",
                    local_filename="overhead.mp4",
                    size_bytes=0,
                )
            ],
            state=AggregationState.PENDING,
        )

        # Patch the global queue helper so we don't actually start the worker thread
        fake_queue = MagicMock(spec=AggregationQueue)
        with patch(
            "syncfield.adapters.insta360_go3s.stream._global_aggregation_queue",
            return_value=fake_queue,
        ):
            session.aggregate_episode("ep_test")
        fake_queue.enqueue.assert_called_once()


@patch("syncfield.adapters.insta360_go3s.stream.Go3SBLECamera")
def test_add_go3s_stream_creates_stream_and_adds_to_orchestrator(_mock_cam, tmp_path):
    """add_go3s_stream creates a Go3SStream with the given address and adds it."""
    from syncfield.adapters.insta360_go3s import Go3SStream
    from syncfield.orchestrator import SessionOrchestrator
    from syncfield.viewer.server import handle_control_command

    session = SessionOrchestrator(host_id="mac", output_dir=tmp_path)
    result = handle_control_command(
        session,
        {
            "command": "add_go3s_stream",
            "address": "AA:BB:CC:DD:EE:FF",
        },
    )
    assert result["ok"] is True
    assert result["stream_id"].startswith("go3s_cam_")
    # Verify the stream was actually registered
    added = session._streams[result["stream_id"]]
    assert isinstance(added, Go3SStream)
    assert added._ble_address == "AA:BB:CC:DD:EE:FF"
