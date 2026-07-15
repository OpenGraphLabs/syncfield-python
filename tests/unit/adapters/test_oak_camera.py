from __future__ import annotations

import importlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def oak_camera_module() -> Any:
    # depthai is a real installed dependency in the SDK test env, so the
    # module imports directly. Pipeline tests monkeypatch this module's ``dai``
    # attribute with a fake locally; there is no global sys.modules swap (which
    # would pollute the shared depthai module for the other OAK test files).
    pytest.importorskip("depthai")
    from syncfield.adapters import oak_camera

    return oak_camera


def test_h264_annex_b_parser_finds_parameter_sets_and_idr(oak_camera_module: Any) -> None:
    data = b"\x00\x00\x00\x01\x67sps\x00\x00\x01\x68pps\x00\x00\x00\x01\x65idr"

    assert oak_camera_module._iter_h264_annex_b_nalus(data) == (
        (7, b"\x00\x00\x00\x01\x67sps"),
        (8, b"\x00\x00\x01\x68pps"),
        (5, b"\x00\x00\x00\x01\x65idr"),
    )
    assert oak_camera_module._contains_h264_parameter_sets(data)
    assert oak_camera_module._contains_h264_nal_type(data, 5)


def test_h264_encoder_segment_tuning_uses_short_keyframe_period(
    oak_camera_module: Any,
) -> None:
    calls: list[tuple[str, int]] = []

    class Encoder:
        def setNumBFrames(self, value: int) -> None:
            calls.append(("bframes", value))

        def setKeyframeFrequency(self, value: int) -> None:
            calls.append(("keyframes", value))

    oak_camera_module._configure_h264_encoder_for_segmented_recording(Encoder(), 30)

    assert calls == [("bframes", 0), ("keyframes", 5)]


class _FakeSessionClock:
    """Minimal SessionClock stand-in — _begin_recording_window only reads
    ``recording_armed_ns``."""

    recording_armed_ns = None


class _EmptyQueue:
    """Stand-in for a live pipeline output queue that yields no packets."""

    def tryGet(self) -> None:
        return None

    def tryGetAll(self) -> list[Any]:
        return []


class _Timestamp:
    def __init__(self, seconds: float = 42.0) -> None:
        self._seconds = seconds

    def total_seconds(self) -> float:
        return self._seconds


class _Packet:
    """Fake encoded-video packet: SPS + PPS + IDR in one payload."""

    def __init__(self, data: bytes | None = None, ts_s: float = 42.0) -> None:
        self._data = (
            data
            if data is not None
            else (b"\x00\x00\x00\x01\x67sps\x00\x00\x01\x68pps\x00\x00\x00\x01\x65idr")
        )
        self._ts_s = ts_s

    def getData(self) -> bytes:
        return self._data

    def getTimestampDevice(self) -> _Timestamp:
        return _Timestamp(self._ts_s)


def test_recording_h264_packet_emits_sample_with_device_timestamp_extra(
    oak_camera_module: Any,
    tmp_path,
) -> None:
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)
    stream._rgb.file = io.BytesIO()
    stream._recording = True

    events = []
    stream.on_sample(events.append)

    stream._handle_camera_packet(stream._rgb, _Packet())

    assert stream._frame_count == 1
    assert stream._rgb.frame_count == 1
    assert events
    assert events[0].frame_number == 0
    if getattr(events[0], "device_ns", None) is not None:
        assert events[0].device_ns == 42_000_000_000
    else:
        assert events[0].channels == {"device_timestamp_ns": 42_000_000_000}


def test_aux_camera_packet_gates_on_keyframe_and_writes_timestamps_row(
    oak_camera_module: Any,
    tmp_path,
) -> None:
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)
    left = stream._left
    left.file = io.BytesIO()
    left.timestamps_file = io.StringIO()
    stream._recording = True

    events = []
    stream.on_sample(events.append)

    # Non-IDR packet before any keyframe: remembered nothing, wrote nothing.
    stream._handle_camera_packet(left, _Packet(b"\x00\x00\x00\x01\x61p-frame"))
    assert left.frame_count == 0
    assert left.file.getvalue() == b""

    stream._handle_camera_packet(left, _Packet(ts_s=7.5))

    assert left.frame_count == 1
    assert b"idr" in left.file.getvalue()
    row = json.loads(left.timestamps_file.getvalue().splitlines()[0])
    assert row["frame_number"] == 0
    assert row["device_timestamp_ns"] == 7_500_000_000
    assert row["clock_source"] == "host_monotonic"
    assert row["capture_ns"] > 0
    # Aux cameras never emit orchestrator samples and never touch the
    # stream-level RGB counters.
    assert events == []
    assert stream._frame_count == 0


class _ImuReport:
    def __init__(self, x: float, y: float, z: float, ts_s: float) -> None:
        self.x = x
        self.y = y
        self.z = z
        self._ts_s = ts_s

    def getTimestampDevice(self) -> _Timestamp:
        return _Timestamp(self._ts_s)


class _RotationVector:
    def __init__(self, i: float, j: float, k: float, real: float, ts_s: float) -> None:
        self.i = i
        self.j = j
        self.k = k
        self.real = real
        self._ts_s = ts_s

    def getTimestampDevice(self) -> _Timestamp:
        return _Timestamp(self._ts_s)


class _ImuPacket:
    def __init__(self) -> None:
        self.acceleroMeter = _ImuReport(0.1, -9.8, 0.2, 1.5)
        self.gyroscope = _ImuReport(0.01, 0.02, -0.03, 1.5)
        self.rotationVector = _RotationVector(0.1, 0.2, 0.3, 0.9, 1.5)


class _ImuMessage:
    def __init__(self, n_packets: int = 2) -> None:
        self.packets = [_ImuPacket() for _ in range(n_packets)]


def test_imu_message_writes_canonical_sensor_rows_while_recording(
    oak_camera_module: Any,
    tmp_path,
) -> None:
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)
    stream._imu.file = io.StringIO()

    # Not recording yet: packets are dropped.
    stream._handle_imu_message(_ImuMessage())
    assert stream._imu.sample_count == 0
    assert stream._imu.file.getvalue() == ""

    stream._recording = True
    stream._handle_imu_message(_ImuMessage(n_packets=2))

    lines = stream._imu.file.getvalue().splitlines()
    assert len(lines) == 2
    assert stream._imu.sample_count == 2
    row = json.loads(lines[0])
    # Canonical SensorSample schema so the sync pipeline's load_sensor_jsonl
    # reads it (requires capture_ns + a `channels` dict).
    assert row["frame_number"] == 0
    assert row["capture_ns"] > 0
    assert row["device_timestamp_ns"] == 1_500_000_000
    assert row["clock_source"] == "host_monotonic"
    channels = row["channels"]
    assert channels["accel_x"] == 0.1
    assert channels["accel_y"] == -9.8
    assert channels["accel_z"] == 0.2
    assert channels["gyro_x"] == 0.01
    assert channels["gyro_z"] == -0.03
    assert channels["quat_x"] == 0.1
    assert channels["quat_y"] == 0.2
    assert channels["quat_z"] == 0.3
    assert channels["quat_w"] == 0.9
    # No legacy top-level vectors leak into the canonical row.
    assert "accel" not in row
    assert "device_ns" not in row


def test_stop_recording_reports_partial_when_aux_stream_has_no_data(
    oak_camera_module: Any,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)
    monkeypatch.setattr(
        oak_camera_module,
        "remux_h264_to_mp4",
        lambda src, dst, *, fps: Path(dst).write_bytes(b"mp4"),
    )
    monkeypatch.setattr(stream, "connect", lambda: None)

    # Left/right are live pipeline outputs that produce zero frames.
    stream._left.queue = _EmptyQueue()
    stream._right.queue = _EmptyQueue()
    stream.start_recording(_FakeSessionClock())
    stream._handle_camera_packet(stream._rgb, _Packet())
    report = stream.stop_recording()

    assert report.status == "partial"
    assert report.error is not None
    assert "left" in report.error and "right" in report.error
    assert report.frame_count == 1
    assert report.file_path == stream._rgb.mp4_path
    stream.disconnect()


def test_stop_recording_remuxes_each_recorded_camera(
    oak_camera_module: Any,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remuxed: list[tuple[str, str, float]] = []

    def fake_remux(src, dst, *, fps: float) -> None:
        remuxed.append((Path(src).name, Path(dst).name, fps))
        Path(dst).write_bytes(b"mp4")

    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path, rgb_fps=30, mono_fps=30)
    monkeypatch.setattr(oak_camera_module, "remux_h264_to_mp4", fake_remux)
    monkeypatch.setattr(stream, "connect", lambda: None)

    stream._left.queue = _EmptyQueue()
    stream._right.queue = _EmptyQueue()
    stream._imu.queue = _EmptyQueue()
    stream.start_recording(_FakeSessionClock())
    stream._handle_camera_packet(stream._rgb, _Packet())
    stream._handle_camera_packet(stream._left, _Packet())
    stream._handle_camera_packet(stream._right, _Packet())
    stream._handle_imu_message(_ImuMessage())
    report = stream.stop_recording()

    assert report.status == "completed"
    assert report.error is None
    assert sorted(name for name, _, _ in remuxed) == [
        "oak_pro.h264",
        "oak_pro.left.h264",
        "oak_pro.right.h264",
    ]
    assert (tmp_path / "oak_pro.left.mp4").exists()
    assert (tmp_path / "oak_pro.right.mp4").exists()
    assert (tmp_path / "oak_pro.imu.jsonl").exists()
    assert (tmp_path / "oak_pro.left.timestamps.jsonl").exists()
    stream.disconnect()


def test_stop_recording_still_fails_when_rgb_has_no_frames(
    oak_camera_module: Any,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)
    monkeypatch.setattr(stream, "connect", lambda: None)
    stream.start_recording(_FakeSessionClock())
    report = stream.stop_recording()

    assert report.status == "failed"
    assert report.error is not None
    stream.disconnect()


class _FakeQueue:
    pass


class _FakeOutput:
    def createOutputQueue(self, *args: Any, **kwargs: Any) -> _FakeQueue:
        return _FakeQueue()


class _FakeCameraControl:
    def __init__(self) -> None:
        self.frame_sync_mode: Any = None

    def setFrameSyncMode(self, mode: Any) -> None:
        self.frame_sync_mode = mode


class _FakeCameraNode:
    def __init__(self) -> None:
        self.built_socket: Any = None
        self.requested: list[tuple[Any, Any, float]] = []
        self.initialControl = _FakeCameraControl()

    def build(self, socket: Any = None) -> None:
        self.built_socket = socket

    def requestOutput(self, size: Any, type: Any, fps: float = 30.0) -> _FakeOutput:
        self.requested.append((size, type, fps))
        return _FakeOutput()


class _FakeEncoderNode:
    def __init__(self) -> None:
        self.build_kwargs: dict[str, Any] = {}
        self.out = _FakeOutput()

    def build(self, **kwargs: Any) -> None:
        self.build_kwargs = kwargs

    def setNumBFrames(self, value: int) -> None:
        pass

    def setKeyframeFrequency(self, value: int) -> None:
        pass

    def setBitrateKbps(self, value: int) -> None:
        self.bitrate_kbps = value


class _FakeImuNode:
    def __init__(self) -> None:
        self.enable_calls: list[tuple[Any, int]] = []
        self.out = _FakeOutput()

    def enableIMUSensor(self, sensors: Any, rate: int) -> None:
        self.enable_calls.append((sensors, rate))

    def setBatchReportThreshold(self, value: int) -> None:
        pass

    def setMaxBatchReports(self, value: int) -> None:
        pass


class _FakePipeline:
    def __init__(self, device: Any) -> None:
        self.device = device
        self.nodes: list[Any] = []

    def create(self, node_cls: Any) -> Any:
        node = node_cls()
        self.nodes.append(node)
        return node


def _fake_dai_v3() -> SimpleNamespace:
    sockets = SimpleNamespace(CAM_A="CAM_A", CAM_B="CAM_B", CAM_C="CAM_C")
    return SimpleNamespace(
        Pipeline=_FakePipeline,
        node=SimpleNamespace(
            Camera=_FakeCameraNode,
            VideoEncoder=_FakeEncoderNode,
            IMU=_FakeImuNode,
        ),
        CameraBoardSocket=sockets,
        ImgFrame=SimpleNamespace(Type=SimpleNamespace(NV12="NV12", BGR888p="BGR888p")),
        VideoEncoderProperties=SimpleNamespace(Profile=SimpleNamespace(H264_MAIN="H264_MAIN")),
        CameraControl=SimpleNamespace(
            FrameSyncMode=SimpleNamespace(OUTPUT="OUTPUT", INPUT="INPUT", OFF="OFF")
        ),
        IMUSensor=SimpleNamespace(
            ACCELEROMETER_RAW="ACC_RAW",
            GYROSCOPE_RAW="GYRO_RAW",
            ROTATION_VECTOR="ROT_VEC",
        ),
    )


class _FakeDeviceFull:
    def getConnectedCameraFeatures(self):
        return [
            SimpleNamespace(socket=SimpleNamespace(name=n)) for n in ("CAM_A", "CAM_B", "CAM_C")
        ]

    def getConnectedIMU(self) -> str:
        return "BNO086"


class _FakeDeviceRgbOnly:
    def getConnectedCameraFeatures(self):
        return [SimpleNamespace(socket=SimpleNamespace(name="CAM_A"))]

    def getConnectedIMU(self) -> str:
        return ""


def test_v3_pipeline_builds_all_streams_on_full_device(
    oak_camera_module: Any,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oak_camera_module, "dai", _fake_dai_v3())
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)

    stream._build_v3_pipeline(_FakeDeviceFull())

    assert stream._rgb.queue is not None
    assert stream._left.queue is not None
    assert stream._right.queue is not None
    assert stream._imu.queue is not None
    assert stream._q_preview is not None


def test_v3_pipeline_requests_640p_mono_30fps_and_400p_rgb_and_combined_imu_enable(
    oak_camera_module: Any,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oak_camera_module, "dai", _fake_dai_v3())
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)

    pipeline = stream._build_v3_pipeline(_FakeDeviceFull())

    cams = [n for n in pipeline.nodes if isinstance(n, _FakeCameraNode)]
    imus = [n for n in pipeline.nodes if isinstance(n, _FakeImuNode)]
    by_socket = {c.built_socket: c for c in cams}
    assert set(by_socket) == {"CAM_A", "CAM_B", "CAM_C"}
    # 30 fps is the priority → mono 1024x640 (640p, native 16:10) so all three
    # streams sustain a clean 30 fps (0 drops, measured) on RVC2; RGB is held at
    # the SAME 30 fps (matched rates keep the ISP interleave regular — a
    # mismatched RGB rate makes the monos jitter). 720p mono tops out at 24 fps
    # on this chip. See the module docstring for the full bandwidth findings.
    assert ((640, 400), "NV12", 30.0) in by_socket["CAM_A"].requested
    assert ((1024, 640), "NV12", 30.0) in by_socket["CAM_B"].requested
    assert ((1024, 640), "NV12", 30.0) in by_socket["CAM_C"].requested
    # IMU must be enabled with ONE combined call (separate enables degrade
    # BNO086 sync output to ~52 Hz — measured on hardware).
    assert len(imus) == 1
    assert imus[0].enable_calls == [(["ACC_RAW", "GYRO_RAW", "ROT_VEC"], 400)]


def test_v3_pipeline_degrades_to_rgb_only_without_stereo_or_imu(
    oak_camera_module: Any,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oak_camera_module, "dai", _fake_dai_v3())
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)

    pipeline = stream._build_v3_pipeline(_FakeDeviceRgbOnly())

    cams = [n for n in pipeline.nodes if isinstance(n, _FakeCameraNode)]
    imus = [n for n in pipeline.nodes if isinstance(n, _FakeImuNode)]
    assert len(cams) == 1
    assert cams[0].built_socket == "CAM_A"
    assert imus == []
    assert stream._rgb.queue is not None
    assert stream._left.queue is None
    assert stream._right.queue is None
    assert stream._imu.queue is None
    # RGB-only board: no stereo pair to follow, so CAM_A must NOT be forced to
    # drive an FSYNC line nobody listens to.
    assert cams[0].initialControl.frame_sync_mode is None


def test_v3_pipeline_enables_hardware_frame_sync_across_the_stereo_triplet(
    oak_camera_module: Any,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAM_A drives FSYNC (OUTPUT), the monos follow (INPUT) so all three
    sensors expose the shutter together — measured ~0.02 ms residual vs
    ~4.6 ms free-running."""
    monkeypatch.setattr(oak_camera_module, "dai", _fake_dai_v3())
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)

    pipeline = stream._build_v3_pipeline(_FakeDeviceFull())

    by_socket = {c.built_socket: c for c in pipeline.nodes if isinstance(c, _FakeCameraNode)}
    assert by_socket["CAM_A"].initialControl.frame_sync_mode == "OUTPUT"
    assert by_socket["CAM_B"].initialControl.frame_sync_mode == "INPUT"
    assert by_socket["CAM_C"].initialControl.frame_sync_mode == "INPUT"


def test_v3_pipeline_rgb_preview_from_h264_skips_isp_preview_output(
    oak_camera_module: Any,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H.264 preview mode requests NO second ISP output on CAM_A: the preview is
    decoded host-side from the encode stream, so the busiest camera does single
    ISP duty and the recording keeps the full ISP budget (all three at 30 fps)."""
    monkeypatch.setattr(oak_camera_module, "dai", _fake_dai_v3())
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path, rgb_preview_from_h264=True)

    pipeline = stream._build_v3_pipeline(_FakeDeviceFull())

    assert stream._q_preview is None
    by_socket = {c.built_socket: c for c in pipeline.nodes if isinstance(c, _FakeCameraNode)}
    formats = [fmt for _, fmt, _ in by_socket["CAM_A"].requested]
    assert "BGR888p" not in formats  # no preview downscale on CAM_A
    assert "NV12" in formats  # encode output still present
    # Recording streams are all unaffected.
    assert stream._rgb.queue is not None
    assert stream._left.queue is not None
    assert stream._right.queue is not None
    assert stream._imu.queue is not None


def test_v3_pipeline_disables_mono_preview_outputs(
    oak_camera_module: Any,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enable_mono_preview=False builds no mono preview downscales (a kiosk that
    only shows the RGB view never uses them), freeing ISP budget for capture."""
    monkeypatch.setattr(oak_camera_module, "dai", _fake_dai_v3())
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path, enable_mono_preview=False)

    pipeline = stream._build_v3_pipeline(_FakeDeviceFull())

    assert stream._substream_preview_queues == {}
    by_socket = {c.built_socket: c for c in pipeline.nodes if isinstance(c, _FakeCameraNode)}
    for socket in ("CAM_B", "CAM_C"):
        formats = [fmt for _, fmt, _ in by_socket[socket].requested]
        assert "BGR888p" not in formats
        assert "NV12" in formats
    # Mono recording queues still built.
    assert stream._left.queue is not None
    assert stream._right.queue is not None


def test_preview_decode_paths_are_best_effort_and_never_raise(
    oak_camera_module: Any,
    tmp_path,
) -> None:
    """Feeding packets and decoding them never raises into the capture thread —
    undecodable input yields None so the endpoint serves its placeholder."""
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)
    # Cheap append; must not raise even before a decoder exists.
    stream._feed_preview_packet(b"\x00\x00\x00\x01garbage")
    assert len(stream._preview_packets) == 1
    # Stream decode with no live decoder returns None, never raises.
    assert stream._decode_stream(None, (b"\x00\x00\x00\x01garbage",)) is None
    # Undecodable bytes through a real (best-effort) decoder still yield None.
    assert stream._decode_stream(stream._new_h264_decoder(), (b"\x00\x00\x00\x01x",)) is None


def test_connected_socket_names_and_imu_detection(oak_camera_module: Any) -> None:
    class Socket:
        def __init__(self, name: str) -> None:
            self.name = name

    class Feature:
        def __init__(self, name: str) -> None:
            self.socket = Socket(name)

    class Device:
        def getConnectedCameraFeatures(self):
            return [Feature("CAM_A"), Feature("CAM_B"), Feature("CAM_C")]

        def getConnectedIMU(self) -> str:
            return "BNO086"

    class BareDevice:
        def getConnectedCameraFeatures(self):
            raise RuntimeError("boom")

        def getConnectedIMU(self) -> str:
            return "NONE"

    assert oak_camera_module._connected_socket_names(Device()) == frozenset(
        {"CAM_A", "CAM_B", "CAM_C"}
    )
    assert oak_camera_module._connected_imu_name(Device()) == "BNO086"
    assert oak_camera_module._connected_socket_names(BareDevice()) is None
    assert oak_camera_module._connected_imu_name(BareDevice()) == ""


def test_substreams_reflect_built_queues(oak_camera_module: Any, tmp_path) -> None:
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)
    assert stream.substreams() == ()

    stream._left.queue = _EmptyQueue()
    stream._right.queue = _EmptyQueue()
    stream._imu.queue = _EmptyQueue()

    subs = stream.substreams()
    assert [(s.id, s.kind) for s in subs] == [
        ("oak_pro.left", "video"),
        ("oak_pro.right", "video"),
        ("oak_pro.imu", "sensor"),
    ]
    assert all(s.label for s in subs)


def test_latest_frame_for_returns_substream_preview_frames(
    oak_camera_module: Any, tmp_path
) -> None:
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)
    assert stream.latest_frame_for("oak_pro.left") is None

    frame = object()
    with stream._frame_lock:
        stream._substream_frames["oak_pro.left"] = frame
    assert stream.latest_frame_for("oak_pro.left") is frame
    assert stream.latest_frame_for("oak_pro.right") is None


def test_imu_messages_emit_live_substream_samples_even_without_recording(
    oak_camera_module: Any, tmp_path
) -> None:
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)
    events: list[Any] = []
    stream.on_substream_sample(events.append)

    stream._handle_imu_message(_ImuMessage(n_packets=2))

    assert len(events) == 2
    event = events[0]
    assert event.stream_id == "oak_pro.imu"
    assert event.frame_number == 0
    assert events[1].frame_number == 1
    assert event.capture_ns > 0
    channels = event.channels
    assert channels["accel_x"] == 0.1
    assert channels["gyro_z"] == -0.03
    assert channels["quat"] == [0.1, 0.2, 0.3, 0.9]
    # Orientation Euler angles derived from the quaternion, in degrees.
    for key in ("roll", "pitch", "yaw"):
        assert isinstance(channels[key], float)
    # File was never opened: nothing recorded, sample_count untouched.
    assert stream._imu.sample_count == 0


def test_second_recording_writes_to_rotated_episode_dir(
    oak_camera_module: Any,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK orchestrator rotates _output_dir per episode (2nd+ recording in
    one connect session) but only rebinds ``stream._output_dir`` — not the
    paths cached inside the per-camera recorders. Every recording must write
    to the CURRENT _output_dir, else consecutive recordings clobber the first
    episode's files and leave the new episode empty."""
    ep1 = tmp_path / "ep1"
    ep2 = tmp_path / "ep2"
    stream = oak_camera_module.OakCameraStream("oak_pro", ep1)
    monkeypatch.setattr(stream, "connect", lambda: None)
    monkeypatch.setattr(
        oak_camera_module,
        "remux_h264_to_mp4",
        lambda src, dst, *, fps: Path(dst).write_bytes(b"mp4"),
    )

    # First recording lands in ep1.
    stream._left.queue = _EmptyQueue()
    stream._imu.queue = _EmptyQueue()
    stream.start_recording(_FakeSessionClock())
    assert stream._rgb.mp4_path == ep1 / "oak_pro.mp4"
    assert stream._left.mp4_path == ep1 / "oak_pro.left.mp4"
    assert stream._imu.path == ep1 / "oak_pro.imu.jsonl"
    stream.stop_recording()

    # Orchestrator rotates the episode dir + rebinds only _output_dir.
    stream._output_dir = ep2

    stream.start_recording(_FakeSessionClock())
    # Every recorder path must now point at ep2, not the stale ep1.
    assert stream._rgb.mp4_path == ep2 / "oak_pro.mp4"
    assert stream._rgb.h264_path == ep2 / "oak_pro.h264"
    assert stream._left.mp4_path == ep2 / "oak_pro.left.mp4"
    assert stream._left.timestamps_path == ep2 / "oak_pro.left.timestamps.jsonl"
    assert stream._right.mp4_path == ep2 / "oak_pro.right.mp4"
    assert stream._imu.path == ep2 / "oak_pro.imu.jsonl"
    stream.disconnect()


def test_recorded_artifacts_lists_aux_streams_after_stop(
    oak_camera_module: Any,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a recording with mono + IMU, the composite reports its aux
    streams so the desktop backend can fold them into the manifest (and
    the sync pipeline can align all four)."""
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)
    monkeypatch.setattr(
        oak_camera_module,
        "remux_h264_to_mp4",
        lambda src, dst, *, fps: Path(dst).write_bytes(b"mp4"),
    )
    monkeypatch.setattr(stream, "connect", lambda: None)

    # Before any recording there are no artifacts.
    assert stream.recorded_artifacts() == ()

    stream._left.queue = _EmptyQueue()
    stream._right.queue = _EmptyQueue()
    stream._imu.queue = _EmptyQueue()
    stream.start_recording(_FakeSessionClock())
    stream._handle_camera_packet(stream._rgb, _Packet())
    stream._handle_camera_packet(stream._left, _Packet())
    stream._handle_camera_packet(stream._right, _Packet())
    stream._recording = True
    stream._handle_imu_message(_ImuMessage(n_packets=3))
    stream.stop_recording()

    arts = {a.stream_id: a for a in stream.recorded_artifacts()}
    # Primary RGB is NOT an aux artifact (it is the orchestrator stream).
    assert "oak_pro" not in arts
    assert set(arts) == {"oak_pro.left", "oak_pro.right", "oak_pro.imu"}
    assert arts["oak_pro.left"].kind == "video"
    assert arts["oak_pro.left"].path == stream._left.mp4_path
    assert arts["oak_pro.left"].frame_count == 1
    assert arts["oak_pro.imu"].kind == "sensor"
    assert arts["oak_pro.imu"].path == stream._imu.path
    assert arts["oak_pro.imu"].frame_count == 3
    stream.disconnect()


def test_recorded_artifacts_omits_aux_streams_with_no_data(
    oak_camera_module: Any,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An aux stream that produced no decodable file must not appear in the
    manifest — otherwise sync would try to align a missing/empty file."""
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)
    monkeypatch.setattr(
        oak_camera_module,
        "remux_h264_to_mp4",
        lambda src, dst, *, fps: Path(dst).write_bytes(b"mp4"),
    )
    monkeypatch.setattr(stream, "connect", lambda: None)

    stream._left.queue = _EmptyQueue()  # live, but never produces a frame
    stream._imu.queue = _EmptyQueue()
    stream.start_recording(_FakeSessionClock())
    stream._handle_camera_packet(stream._rgb, _Packet())
    stream.stop_recording()

    # left produced no keyframe, imu produced no samples → no artifacts.
    assert stream.recorded_artifacts() == ()
    stream.disconnect()


def test_quat_to_euler_identity_and_yaw(oak_camera_module: Any) -> None:
    import math

    roll, pitch, yaw = oak_camera_module._quat_to_euler_deg(0.0, 0.0, 0.0, 1.0)
    assert (round(roll, 6), round(pitch, 6), round(yaw, 6)) == (0.0, 0.0, 0.0)
    # 90° yaw about Z: q = (0, 0, sin45, cos45)
    s = math.sin(math.pi / 4)
    c = math.cos(math.pi / 4)
    roll, pitch, yaw = oak_camera_module._quat_to_euler_deg(0.0, 0.0, s, c)
    assert abs(yaw - 90.0) < 1e-6
    assert abs(roll) < 1e-6 and abs(pitch) < 1e-6


class _FakeCalibDevice:
    """Device + CalibrationHandler double for calibration summary tests."""

    class _Calib:
        def eepromToJson(self) -> dict[str, Any]:
            return {"version": 7, "boardName": "DM9098"}

        def getCameraIntrinsics(self, socket: Any, width: int = 0, height: int = 0):
            scale = 0.5 if width == 640 else 1.0
            return [
                [565.0 * scale, 0.0, 640.0 * scale],
                [0.0, 565.0 * scale, 400.0 * scale],
                [0.0, 0.0, 1.0],
            ]

        def getDistortionCoefficients(self, socket: Any):
            return [0.1, 0.2]

        def getFov(self, socket: Any):
            return 127.0

        def getCameraExtrinsics(self, src: Any, dst: Any):
            return [
                [1.0, 0.0, 0.0, -7.5],
                [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ]

        def getImuToCameraExtrinsics(self, dst: Any):
            return [
                [0.0, 1.0, 0.0, 7.75],
                [1.0, 0.0, 0.0, -0.2],
                [0.0, 0.0, -1.0, 2.0],
                [0.0, 0.0, 0.0, 1.0],
            ]

        def getBaselineDistance(self) -> float:
            return 7.5

    def readCalibration(self) -> _FakeCalibDevice._Calib:
        return self._Calib()

    def getDeviceName(self) -> str:
        return "OAK-D-PRO-W"

    def getProductName(self) -> str:
        return "OAK-D-PRO-W-97"

    def getDeviceId(self) -> str:
        return "1944301091E3965A00"

    def getConnectedIMU(self) -> str:
        return "BNO086"


def test_calibration_summary_scales_intrinsics_to_recorded_resolution(
    oak_camera_module: Any,
) -> None:
    summary = oak_camera_module._build_calibration_summary(
        _FakeCalibDevice(),
        [
            ("rgb", "CAM_A", "socketA", (1280, 800), 30.0),
            ("left", "CAM_B", "socketB", (640, 400), 30.0),
            ("right", "CAM_C", "socketC", (640, 400), 30.0),
        ],
    )

    assert summary is not None
    assert summary["schema"] == "syncfield.oak_calibration.v1"
    assert summary["device"]["device_id"] == "1944301091E3965A00"
    assert summary["eeprom"] == {"version": 7, "boardName": "DM9098"}
    rgb = summary["streams"]["rgb"]
    assert rgb["socket"] == "CAM_A"
    assert rgb["resolution"] == [1280, 800]
    assert rgb["intrinsics"][0][0] == 565.0
    left = summary["streams"]["left"]
    assert left["resolution"] == [640, 400]
    assert left["intrinsics"][0][0] == 282.5  # scaled to 640x400
    assert summary["stereo"]["baseline_cm"] == 7.5
    assert summary["imu"]["type"] == "BNO086"
    assert "extrinsics" in summary


def test_calibration_summary_returns_none_when_unreadable(
    oak_camera_module: Any,
) -> None:
    class BrokenDevice:
        def readCalibration(self):
            raise RuntimeError("no eeprom")

    assert (
        oak_camera_module._build_calibration_summary(
            BrokenDevice(), [("rgb", "CAM_A", "socketA", (1280, 800), 30.0)]
        )
        is None
    )


def test_start_recording_writes_calibration_json(
    oak_camera_module: Any,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)
    monkeypatch.setattr(stream, "connect", lambda: None)
    stream._calibration = {"schema": "syncfield.oak_calibration.v1", "x": 1}

    stream.start_recording(_FakeSessionClock())
    stream._recording = False

    path = tmp_path / "oak_pro.calibration.json"
    assert path.exists()
    assert json.loads(path.read_text())["x"] == 1


def test_v3_pipeline_builds_mono_preview_queues(
    oak_camera_module: Any,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oak_camera_module, "dai", _fake_dai_v3())
    stream = oak_camera_module.OakCameraStream("oak_pro", tmp_path)

    pipeline = stream._build_v3_pipeline(_FakeDeviceFull())

    cams = [n for n in pipeline.nodes if isinstance(n, _FakeCameraNode)]
    by_socket = {c.built_socket: c for c in cams}
    # Monos now request the encoder feed AND a small BGR preview.
    assert ((1024, 640), "NV12", 30.0) in by_socket["CAM_B"].requested
    assert ((320, 200), "BGR888p", 10.0) in by_socket["CAM_B"].requested
    assert ((320, 200), "BGR888p", 10.0) in by_socket["CAM_C"].requested
    assert set(stream._substream_preview_queues) == {"oak_pro.left", "oak_pro.right"}


def test_sample_event_falls_back_to_channels_for_legacy_syncfield(
    oak_camera_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LegacySampleEvent:
        def __init__(
            self,
            stream_id: str,
            frame_number: int,
            capture_ns: int,
            channels: dict[str, int] | None = None,
        ) -> None:
            self.stream_id = stream_id
            self.frame_number = frame_number
            self.capture_ns = capture_ns
            self.channels = channels

    monkeypatch.setattr(oak_camera_module, "SampleEvent", LegacySampleEvent)
    monkeypatch.setattr(oak_camera_module, "_SAMPLE_EVENT_SUPPORTS_DEVICE_NS", False)

    event = oak_camera_module._sample_event(
        stream_id="oak_pro",
        frame_number=0,
        capture_ns=1,
        device_ts_ns=42_000_000_000,
    )

    assert event.channels == {"device_timestamp_ns": 42_000_000_000}
