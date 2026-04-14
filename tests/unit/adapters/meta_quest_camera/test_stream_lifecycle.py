"""Unit tests for the top-level MetaQuestCameraStream adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncfield.adapters.meta_quest_camera import MetaQuestCameraStream


class TestIdentity:
    def test_stream_identity_and_capabilities(self, tmp_path: Path):
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="192.0.2.10",
            output_dir=tmp_path,
        )
        assert stream.id == "quest_cam"
        assert stream.kind == "video"
        assert stream.capabilities.produces_file is True
        assert stream.capabilities.supports_precise_timestamps is True
        assert stream.capabilities.is_removable is True
        assert stream.capabilities.provides_audio_track is False

    def test_device_key_includes_host(self, tmp_path: Path):
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="192.0.2.10",
            output_dir=tmp_path,
        )
        assert stream.device_key == ("meta_quest_camera", "192.0.2.10")
