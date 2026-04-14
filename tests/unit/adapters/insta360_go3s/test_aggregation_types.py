from pathlib import Path

from syncfield.adapters.insta360_go3s.aggregation.types import (
    AggregationCameraSpec,
    AggregationJob,
    AggregationProgress,
    AggregationState,
)


def test_state_values():
    assert AggregationState.PENDING.value == "pending"
    assert AggregationState.RUNNING.value == "running"
    assert AggregationState.COMPLETED.value == "completed"
    assert AggregationState.FAILED.value == "failed"


def test_camera_spec_round_trips_dict():
    spec = AggregationCameraSpec(
        stream_id="overhead",
        ble_address="AA:BB",
        wifi_ssid="Go3S-CAFEBABE.OSC",
        wifi_password="88888888",
        sd_path="/DCIM/Camera01/VID_FAKE.mp4",
        local_filename="overhead.mp4",
        size_bytes=12,
        done=False,
    )
    d = spec.to_dict()
    restored = AggregationCameraSpec.from_dict(d)
    assert restored == spec


def test_job_to_dict_includes_all_cameras(tmp_path):
    job = AggregationJob(
        job_id="agg_x",
        episode_id="ep_x",
        episode_dir=tmp_path,
        cameras=[
            AggregationCameraSpec(
                stream_id="overhead",
                ble_address="AA:BB",
                wifi_ssid="Go3S-X.OSC",
                wifi_password="88888888",
                sd_path="/DCIM/Camera01/VID.mp4",
                local_filename="overhead.mp4",
                size_bytes=0,
                done=False,
            )
        ],
        state=AggregationState.PENDING,
    )
    d = job.to_dict()
    assert d["job_id"] == "agg_x"
    assert d["episode_id"] == "ep_x"
    assert len(d["cameras"]) == 1
    assert d["state"] == "pending"


def test_progress_dataclass_defaults():
    p = AggregationProgress(
        job_id="agg_x",
        episode_id="ep_x",
        state=AggregationState.RUNNING,
        cameras_total=2,
        cameras_done=0,
    )
    assert p.current_stream_id is None
    assert p.current_bytes == 0
    assert p.current_total_bytes == 0
    assert p.error is None
