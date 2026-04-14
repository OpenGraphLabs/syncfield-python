"""MetaQuestCameraStream — SyncField adapter for Quest 3 stereo passthrough cameras."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

from syncfield.stream import DeviceKey, StreamBase
from syncfield.types import StreamCapabilities


logger = logging.getLogger(__name__)


# Matches the Quest companion Unity app's default HTTP port (spec §2).
DEFAULT_QUEST_HTTP_PORT = 14045
DEFAULT_FPS = 30
DEFAULT_RESOLUTION: Tuple[int, int] = (1280, 720)


class MetaQuestCameraStream(StreamBase):
    """Captures Meta Quest 3 stereo passthrough cameras (hybrid mode).

    Live: low-res MJPEG preview pulled from the Quest for the viewer.
    Recorded: 720p×30 H.264 recorded on the Quest, pulled to
    ``output_dir`` after :meth:`stop_recording` completes.

    See ``docs/superpowers/specs/2026-04-13-metaquest-stereo-camera-design.md``
    for the full protocol + architecture notes.
    """

    CLOCK_DOMAIN = "remote_quest3"
    UNCERTAINTY_NS = 10_000_000  # 10 ms — WiFi jitter budget, matches MetaQuestHandStream

    def __init__(
        self,
        id: str,
        *,
        quest_host: str,
        output_dir: Path,
        quest_port: int = DEFAULT_QUEST_HTTP_PORT,
        fps: int = DEFAULT_FPS,
        resolution: Tuple[int, int] = DEFAULT_RESOLUTION,
    ) -> None:
        super().__init__(
            id=id,
            kind="video",
            capabilities=StreamCapabilities(
                provides_audio_track=False,
                supports_precise_timestamps=True,
                is_removable=True,
                produces_file=True,
            ),
        )
        self._quest_host = quest_host
        self._quest_port = quest_port
        self._fps = fps
        self._resolution = resolution
        self._output_dir = Path(output_dir)

    @property
    def device_key(self) -> Optional[DeviceKey]:
        return ("meta_quest_camera", self._quest_host)
