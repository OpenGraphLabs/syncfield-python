"""MetaQuestCameraStream — SyncField adapter for Quest 3 stereo passthrough cameras."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional, Tuple

import httpx

from syncfield.adapters.meta_quest_camera.http_client import QuestHttpClient
from syncfield.adapters.meta_quest_camera.preview import MjpegPreviewConsumer
from syncfield.stream import DeviceKey, StreamBase
from syncfield.types import HealthEvent, HealthEventKind, StreamCapabilities


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
        _transport: Optional[httpx.BaseTransport] = None,
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
        self._transport = _transport
        self._http: Optional[QuestHttpClient] = None
        self._preview_left: Optional[MjpegPreviewConsumer] = None
        self._preview_right: Optional[MjpegPreviewConsumer] = None
        self._connected = False

    @property
    def device_key(self) -> Optional[DeviceKey]:
        return ("meta_quest_camera", self._quest_host)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        if self._connected:
            return
        self._http = QuestHttpClient(
            host=self._quest_host,
            port=self._quest_port,
            transport=self._transport,
        )
        # Probe reachability up front so failures surface before recording starts.
        self._http.status()
        self._preview_left = self._make_preview("left")
        self._preview_right = self._make_preview("right")
        self._preview_left.start()
        self._preview_right.start()
        self._connected = True
        logger.info(
            "[%s] connected to Quest %s:%d",
            self.id, self._quest_host, self._quest_port,
        )

    def disconnect(self) -> None:
        if self._preview_left is not None:
            self._preview_left.stop()
            self._preview_left = None
        if self._preview_right is not None:
            self._preview_right.stop()
            self._preview_right = None
        if self._http is not None:
            self._http.close()
            self._http = None
        self._connected = False

    # ------------------------------------------------------------------

    def _make_preview(self, side: str) -> MjpegPreviewConsumer:
        url = f"http://{self._quest_host}:{self._quest_port}/preview/{side}"

        def _on_health(kind: str, detail: str) -> None:
            mapping = {
                "drop": HealthEventKind.DROP,
                "reconnect": HealthEventKind.RECONNECT,
                "warning": HealthEventKind.WARNING,
            }
            self._emit_health(
                HealthEvent(
                    stream_id=self.id,
                    kind=mapping.get(kind, HealthEventKind.WARNING),
                    at_ns=time.monotonic_ns(),
                    detail=f"[{side}] {detail}",
                )
            )

        return MjpegPreviewConsumer(
            url=url,
            boundary=b"syncfield",
            transport=self._transport,
            decode_jpeg=True,
            on_health=_on_health,
        )
