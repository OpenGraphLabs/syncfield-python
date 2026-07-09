"""OGLO glove extrinsic calibration model.

The stored OGLO calibration (``syncfield.oglo_calibration.v1``) carries the
marker and wrist-IMU extrinsics for BOTH hands of a case model — the geometry is
pure CAD, shared by every glove of that model. A recorded glove writes only its
own side, so :func:`oglo_side_document` flattens the both-hands doc into a clean
single-side document (shared top-level fields + the one hand's block), mirroring
the per-camera calibration file. Kept dependency-free so both the adapter (which
writes the file) and the desktop app (which builds/stores the doc) can import it.

Convention (as stored): ``T_wrist_from_marker`` — ``p_wrist = R @ p_marker + t``
maps a point from a marker's frame into the wrist frame; ``R`` is 3x3 row-major,
``t`` in meters. Usage: given a camera detection ``T_cam_from_marker[i]``, the
wrist pose is ``T_cam_from_marker[i] @ inv(T_wrist_from_marker[i])``, fused over
all detected markers.
"""

from __future__ import annotations

from typing import Any, Optional

#: Schema id stamped on every OGLO calibration document.
OGLO_CALIBRATION_SCHEMA = "syncfield.oglo_calibration.v1"

#: Top-level fields shared across both hands (copied into each per-side doc).
_SHARED_KEYS = ("schema", "rig_id", "units", "convention", "usage", "source")


def oglo_side_document(
    full: Optional[dict[str, Any]], side: str
) -> Optional[dict[str, Any]]:
    """Flatten a both-hands OGLO calibration into a single-side document.

    Returns ``None`` when ``full`` is falsy or ``side`` has no hand block, so a
    caller can skip writing rather than emit a malformed file.
    """
    if not full or not isinstance(full, dict):
        return None
    hand = (full.get("hands") or {}).get(side)
    if not isinstance(hand, dict):
        return None
    out: dict[str, Any] = {key: full[key] for key in _SHARED_KEYS if key in full}
    out["side"] = side
    # Hoist the hand block (wrist_frame / markers / imu / ...) to the top level.
    for key, value in hand.items():
        out[key] = value
    return out
