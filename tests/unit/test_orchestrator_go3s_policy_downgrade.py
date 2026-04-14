from pathlib import Path
from unittest.mock import patch

import pytest

from syncfield.adapters.insta360_go3s import Go3SStream
from syncfield.orchestrator import SessionOrchestrator
from syncfield.roles import LeaderRole, FollowerRole


@patch("syncfield.adapters.insta360_go3s.stream.Go3SBLECamera")
def test_eager_downgrades_to_on_demand_when_leader(_mock_cam, tmp_path):
    session = SessionOrchestrator(
        host_id="mac",
        output_dir=tmp_path,
        role=LeaderRole(session_id="sess_x"),
    )
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
        aggregation_policy="eager",
    )
    session.add(s)
    assert s._aggregation_policy == "on_demand"


@patch("syncfield.adapters.insta360_go3s.stream.Go3SBLECamera")
def test_eager_downgrades_to_on_demand_when_follower(_mock_cam, tmp_path):
    session = SessionOrchestrator(
        host_id="mac",
        output_dir=tmp_path,
        role=FollowerRole(session_id="sess_x"),
    )
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
        aggregation_policy="eager",
    )
    session.add(s)
    assert s._aggregation_policy == "on_demand"


@patch("syncfield.adapters.insta360_go3s.stream.Go3SBLECamera")
def test_eager_unchanged_when_single_host(_mock_cam, tmp_path):
    session = SessionOrchestrator(host_id="mac", output_dir=tmp_path)
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
        aggregation_policy="eager",
    )
    session.add(s)
    assert s._aggregation_policy == "eager"


@patch("syncfield.adapters.insta360_go3s.stream.Go3SBLECamera")
def test_on_demand_unchanged_when_leader(_mock_cam, tmp_path):
    """Explicit on_demand stays on_demand even with leader role."""
    session = SessionOrchestrator(
        host_id="mac",
        output_dir=tmp_path,
        role=LeaderRole(session_id="sess_x"),
    )
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
        aggregation_policy="on_demand",
    )
    session.add(s)
    assert s._aggregation_policy == "on_demand"
