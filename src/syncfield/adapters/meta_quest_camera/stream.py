"""MetaQuestCameraStream — SyncField adapter for Quest 3 stereo passthrough cameras."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

import time

import httpx

from syncfield.adapters.meta_quest_camera.file_puller import RecordingFilePuller
from syncfield.adapters.meta_quest_camera.http_client import QuestHttpClient
from syncfield.adapters.meta_quest_camera.preview import MjpegPreviewConsumer
from syncfield.adapters.meta_quest_camera.timestamps import TimestampTailReader
from syncfield.clock import SessionClock
from syncfield.stream import DeviceKey, StreamBase
from syncfield.types import FinalizationReport, HealthEvent, HealthEventKind, SampleEvent, StreamCapabilities


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
        self._timestamp_tail: Optional[TimestampTailReader] = None
        self._session_id: Optional[str] = None
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None
        self._frame_count = 0

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

    def prepare(self) -> None:
        pass

    def start_recording(self, session_clock: SessionClock) -> None:
        if self._http is None:
            raise RuntimeError("start_recording() called before connect()")

        self._session_id = (
            f"ep_{session_clock.sync_point.timestamp_ms}"
            f"_{session_clock.sync_point.host_id}"
        )
        self._frame_count = 0
        self._first_at = None
        self._last_at = None

        self._http.start_recording(
            session_id=self._session_id,
            host_mono_ns=session_clock.sync_point.monotonic_ns,
            width=self._resolution[0],
            height=self._resolution[1],
            fps=self._fps,
        )

        # Tail the LEFT eye's chunked timestamps endpoint; right eye's exact
        # per-frame ts lives in the authoritative JSONL written by the puller.
        url = (
            f"http://{self._quest_host}:{self._quest_port}"
            f"/recording/timestamps/left"
        )
        self._timestamp_tail = TimestampTailReader(
            url=url,
            stream_id=self.id,
            on_sample=self._handle_tail_sample,
            transport=self._transport,
            clock_domain=self.CLOCK_DOMAIN,
            uncertainty_ns=self.UNCERTAINTY_NS,
        )
        self._timestamp_tail.start()

    def stop_recording(self) -> FinalizationReport:
        if self._http is None:
            raise RuntimeError("stop_recording() called before connect()")

        try:
            stop_response = self._http.stop_recording()
            if self._timestamp_tail is not None:
                self._timestamp_tail.stop()
                self._timestamp_tail = None

            puller = RecordingFilePuller(
                client=self._http, stream_id=self.id, output_dir=self._output_dir
            )
            artifacts = puller.pull_all()

            # Verify file sizes match the /stop response (spec §4.3).
            size_errors = []
            left_actual = artifacts.left_mp4.stat().st_size
            if left_actual != stop_response.left.bytes:
                size_errors.append(
                    f"left size mismatch: expected {stop_response.left.bytes} bytes,"
                    f" got {left_actual} bytes on disk"
                )
            right_actual = artifacts.right_mp4.stat().st_size
            if right_actual != stop_response.right.bytes:
                size_errors.append(
                    f"right size mismatch: expected {stop_response.right.bytes} bytes,"
                    f" got {right_actual} bytes on disk"
                )

            if size_errors:
                status = "partial"
                error: Optional[str] = "; ".join(size_errors)
                logger.warning(
                    "[%s] Recording files may be truncated: %s", self.id, error
                )
            else:
                # All good — tell the Quest to clean up the session files.
                self._http.delete_recording()
                status = "completed"
                error = None
        except Exception as exc:
            status = "failed"
            error = str(exc)
            artifacts = None

        return FinalizationReport(
            stream_id=self.id,
            status=status,
            frame_count=self._frame_count,
            file_path=artifacts.left_mp4 if artifacts is not None else None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=error,
        )

    def _handle_tail_sample(self, event: SampleEvent) -> None:
        if self._first_at is None:
            self._first_at = event.capture_ns
        self._last_at = event.capture_ns
        self._frame_count += 1
        self._emit_sample(event)

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

    @property
    def latest_frame_left(self):
        """Most-recent decoded BGR preview frame from the left camera, or None."""
        if self._preview_left is None:
            return None
        return self._preview_left.latest_frame

    @property
    def latest_frame_right(self):
        if self._preview_right is None:
            return None
        return self._preview_right.latest_frame

    @property
    def latest_frame(self):
        """Viewer-compat: return a side-by-side ``[left | right]`` composite
        so the single video panel shows both eyes at once. The viewer's
        ``StreamSnapshot`` polls ``stream.latest_frame`` for any adapter
        declaring ``kind="video"``; syncfield's panel model is 1 panel =
        1 stream_id, so until we split into two adapters we surface the
        stereo pair as a horizontally-concatenated frame.

        Falls back to whichever eye is available if the other is still
        connecting or has dropped — users should see *something* rather
        than a black card whenever at least one preview is alive.
        """
        left = self.latest_frame_left
        right = self.latest_frame_right
        if left is not None and right is not None:
            import numpy as np
            if left.shape == right.shape:
                return np.hstack((left, right))
            # Shapes can diverge for a frame or two during startup while
            # the two previews race to produce their first decoded image.
            # Fall through to the single-eye path instead of raising.
        if left is not None:
            return left
        return right
