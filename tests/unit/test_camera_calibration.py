"""Shared camera-calibration abstraction: CameraCalibration + writer.

One serializer for intrinsic camera calibration, reused by every camera
adapter (UVC measured, OAK eeprom, imported). Produces the
``syncfield.camera_calibration.v1`` schema and writes the
``{stream_id}.calibration.json`` episode sidecar (mirroring the OAK layout).
"""

from __future__ import annotations

import json

from syncfield.calibration import (
    CAMERA_CALIBRATION_SCHEMA,
    CameraCalibration,
    write_calibration_file,
)


def _calib(**over) -> CameraCalibration:
    base = dict(
        camera_matrix=[[560.0, 0.0, 640.0], [0.0, 560.0, 400.0], [0.0, 0.0, 1.0]],
        distortion_coefficients=[0.1, -0.05, 0.0, 0.0, 0.0],
        resolution=(1280, 800),
        model="Generic UVC",
        source="measured",
        rms_reprojection_error=0.31,
        measured_at="2026-07-07T12:00:00Z",
        image_count=18,
        board={"type": "charuco", "squares_x": 12, "squares_y": 9},
    )
    base.update(over)
    return CameraCalibration(**base)


class TestCameraCalibration:
    def test_holds_fields(self):
        c = _calib()
        assert c.camera_matrix[0][0] == 560.0
        assert c.resolution == (1280, 800)
        assert c.source == "measured"

    def test_to_dict_uses_shared_schema_and_provenance(self):
        d = _calib().to_dict()
        assert d["schema"] == CAMERA_CALIBRATION_SCHEMA
        assert d["schema"] == "syncfield.camera_calibration.v1"
        assert d["resolution"] == [1280, 800]  # serialised as a list
        assert d["camera_matrix"][1][1] == 560.0
        prov = d["provenance"]
        assert prov["source"] == "measured"
        assert prov["rms_reprojection_error"] == 0.31
        assert prov["image_count"] == 18
        assert prov["board"]["type"] == "charuco"

    def test_extra_block_is_merged_top_level(self):
        # OAK reuses the shared core and folds stereo/imu/eeprom via `extra`.
        d = _calib(extra={"stereo": {"baseline_cm": 7.5}}).to_dict()
        assert d["stereo"]["baseline_cm"] == 7.5

    def test_from_dict_round_trip(self):
        d = _calib().to_dict()
        c2 = CameraCalibration.from_dict(d)
        assert c2.camera_matrix == _calib().camera_matrix
        assert c2.resolution == (1280, 800)
        assert c2.rms_reprojection_error == 0.31


class TestWriteCalibrationFile:
    def test_writes_named_sidecar(self, tmp_path):
        path = write_calibration_file(tmp_path, "ego_center", _calib())
        assert path == tmp_path / "ego_center.calibration.json"
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded["schema"] == CAMERA_CALIBRATION_SCHEMA
        assert loaded["camera_matrix"][0][2] == 640.0
