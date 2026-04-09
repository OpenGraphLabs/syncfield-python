"""Unit tests for the snake_case stream-id generator."""

from __future__ import annotations

import pytest

from syncfield.discovery._id_gen import make_stream_id, normalize


class TestNormalize:
    def test_plain_lowercase(self):
        assert normalize("camera") == "camera"

    def test_mixed_case(self):
        assert normalize("FaceTime HD Camera") == "facetime_hd_camera"

    def test_hyphens_and_punctuation(self):
        assert normalize("OAK-D S2") == "oak_d_s2"
        assert normalize("oglo.glove [right]") == "oglo_glove_right"

    def test_collapses_runs_of_underscores(self):
        assert normalize("camera   main") == "camera_main"
        assert normalize("a!!!b@@@c") == "a_b_c"

    def test_strips_leading_trailing(self):
        assert normalize("  camera  ") == "camera"
        assert normalize("__cam__") == "cam"

    def test_empty_returns_fallback(self):
        assert normalize("") == "device"
        assert normalize("!!!") == "device"

    def test_unicode_non_ascii_becomes_underscores(self):
        """Non-ASCII letters are not alphanumeric under our regex — that's
        intentional so generated ids stay ASCII-safe."""
        assert normalize("카메라") == "device"


class TestMakeStreamId:
    def test_no_collision(self):
        assert make_stream_id("FaceTime HD Camera", set()) == "facetime_hd_camera"

    def test_collision_appends_zero_then_one(self):
        taken = {"camera"}
        assert make_stream_id("camera", taken) == "camera_0"

        taken.add("camera_0")
        assert make_stream_id("camera", taken) == "camera_1"

    def test_prefix(self):
        result = make_stream_id("camera", set(), prefix="rig_01")
        assert result == "rig_01_camera"

    def test_prefix_is_also_normalized(self):
        result = make_stream_id("cam", set(), prefix="Rig 01!")
        assert result == "rig_01_cam"

    def test_existing_ids_can_be_any_iterable(self):
        make_stream_id("cam", ["cam", "other"])
        make_stream_id("cam", frozenset({"cam"}))
        make_stream_id("cam", (x for x in ["cam"]))  # generator

    def test_exhaustion_raises(self):
        taken = {"camera"} | {f"camera_{i}" for i in range(100)}
        with pytest.raises(RuntimeError, match="collision"):
            make_stream_id("camera", taken)
