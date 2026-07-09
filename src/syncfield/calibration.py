"""Shared intrinsic camera-calibration abstraction.

One type and one writer for camera calibration across every adapter — UVC
(measured on-device), OAK (read from EEPROM), or imported factory values.
Previously the only serializer lived inside the OAK adapter; this centralises
it so a single ``syncfield.camera_calibration.v1`` document (camera matrix +
distortion + provenance) is produced everywhere and written as the
``{stream_id}.calibration.json`` episode sidecar.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

CAMERA_CALIBRATION_SCHEMA = "syncfield.camera_calibration.v1"

CalibrationSource = Literal["measured", "eeprom", "imported"]


@dataclass(frozen=True)
class CameraCalibration:
    """Intrinsic calibration for a single camera/lens.

    Intrinsics are scale-invariant, so ``board``'s metric size is recorded for
    provenance only and is not required for correctness. ``extra`` carries
    adapter-specific extensions (OAK stereo/imu/eeprom) that ride on top of the
    shared core without changing the schema id.

    Attributes:
        camera_matrix: 3x3 intrinsic matrix [[fx,0,cx],[0,fy,cy],[0,0,1]].
        distortion_coefficients: OpenCV distortion vector (k1,k2,p1,p2,k3,...).
        resolution: (width, height) the calibration was measured at.
        model: Human-readable camera/product name, if known.
        source: How the calibration was obtained.
        rms_reprojection_error: Mean reprojection error in pixels (quality).
        measured_at: ISO-8601 UTC timestamp of measurement, if applicable.
        image_count: Number of views used (for ``measured``).
        board: Calibration-board descriptor (type/dimensions), for provenance.
        extra: Adapter-specific extension blocks, merged at top level on dump.
    """

    camera_matrix: list[list[float]]
    distortion_coefficients: list[float]
    resolution: tuple[int, int]
    model: str | None = None
    # Distortion/projection model — the DOWNSTREAM CONTRACT for undistortion:
    # ``opencv_radtan`` / ``opencv_rational`` are pinhole (cv2.undistort);
    # ``kannala_brandt`` is fisheye (cv2.fisheye.undistort*). Defaults to
    # radtan for backward compatibility with calibrations written before this
    # field existed.
    distortion_model: str = "opencv_radtan"
    source: CalibrationSource = "measured"
    rms_reprojection_error: float | None = None
    measured_at: str | None = None
    image_count: int | None = None
    board: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": CAMERA_CALIBRATION_SCHEMA,
            "camera_matrix": [list(map(float, row)) for row in self.camera_matrix],
            "distortion_coefficients": [float(x) for x in self.distortion_coefficients],
            "resolution": [int(self.resolution[0]), int(self.resolution[1])],
            "model": self.model,
            "distortion_model": self.distortion_model,
            "provenance": {
                "source": self.source,
                "rms_reprojection_error": self.rms_reprojection_error,
                "measured_at": self.measured_at,
                "image_count": self.image_count,
                "board": self.board,
            },
        }
        # Adapter extensions (e.g. OAK stereo/imu/eeprom) fold in at top level.
        for key, value in self.extra.items():
            if key not in out:
                out[key] = value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CameraCalibration":
        prov = data.get("provenance", {})
        res = data.get("resolution", [0, 0])
        known = {
            "schema",
            "camera_matrix",
            "distortion_coefficients",
            "resolution",
            "model",
            "distortion_model",
            "provenance",
        }
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(
            camera_matrix=[list(map(float, row)) for row in data["camera_matrix"]],
            distortion_coefficients=[float(x) for x in data["distortion_coefficients"]],
            resolution=(int(res[0]), int(res[1])),
            model=data.get("model"),
            distortion_model=data.get("distortion_model", "opencv_radtan"),
            source=prov.get("source", "imported"),
            rms_reprojection_error=prov.get("rms_reprojection_error"),
            measured_at=prov.get("measured_at"),
            image_count=prov.get("image_count"),
            board=prov.get("board"),
            extra=extra,
        )


def write_calibration_file(
    output_dir: Path, stream_id: str, calib: CameraCalibration
) -> Path:
    """Write ``{stream_id}.calibration.json`` into ``output_dir`` and return it.

    Matches the OAK adapter's sidecar layout so downstream consumers and the
    episode library scanner treat UVC and OAK calibration identically.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{stream_id}.calibration.json"
    path.write_text(json.dumps(calib.to_dict(), indent=2))
    return path
