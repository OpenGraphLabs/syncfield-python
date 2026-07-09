"""OGLO calibration side-document hoisting.

The stored OGLO calibration carries both hands; each recorded glove writes only
its own side. ``oglo_side_document`` flattens the full both-hands doc into a
clean single-side document (the top-level shared fields + the one hand's block),
mirroring the per-camera calibration file shape.
"""

from __future__ import annotations

from syncfield.oglo_calibration import OGLO_CALIBRATION_SCHEMA, oglo_side_document

FULL = {
    "schema": OGLO_CALIBRATION_SCHEMA,
    "rig_id": "OGLO-MT-CASE-07",
    "units": "meters",
    "convention": "T_wrist_from_marker",
    "usage": "wrist pose from detection",
    "hands": {
        "right": {
            "wrist_frame": "right_wrist",
            "markers": [{"id": 4}, {"id": 5}, {"id": 6}, {"id": 7}],
            "imu": {"part": "ICM-42688-P", "orientation_confirmed": True},
        },
        "left": {
            "wrist_frame": "left_wrist",
            "markers": [{"id": 0}, {"id": 1}, {"id": 2}, {"id": 3}],
            "imu": {"part": "ICM-42688-P", "orientation_confirmed": False, "R": None},
        },
    },
    "source": {"file": "oglo_marker_extrinsics.json"},
}


def test_side_document_hoists_the_hand_block():
    doc = oglo_side_document(FULL, "right")
    assert doc["schema"] == OGLO_CALIBRATION_SCHEMA
    assert doc["rig_id"] == "OGLO-MT-CASE-07"
    assert doc["units"] == "meters"
    assert doc["convention"] == "T_wrist_from_marker"
    assert doc["source"]["file"] == "oglo_marker_extrinsics.json"
    # The hand block is flattened onto this side; no nested "hands".
    assert doc["side"] == "right"
    assert doc["wrist_frame"] == "right_wrist"
    assert [m["id"] for m in doc["markers"]] == [4, 5, 6, 7]
    assert doc["imu"]["orientation_confirmed"] is True
    assert "hands" not in doc


def test_side_document_left_preserves_partial_imu():
    doc = oglo_side_document(FULL, "left")
    assert doc["side"] == "left"
    assert [m["id"] for m in doc["markers"]] == [0, 1, 2, 3]
    assert doc["imu"]["orientation_confirmed"] is False
    assert doc["imu"]["R"] is None


def test_side_document_unknown_or_empty_is_none():
    assert oglo_side_document(FULL, "middle") is None
    assert oglo_side_document({}, "right") is None
    assert oglo_side_document(None, "right") is None
    assert oglo_side_document(FULL, "unknown") is None
