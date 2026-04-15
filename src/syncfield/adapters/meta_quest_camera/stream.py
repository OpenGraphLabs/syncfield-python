"""MetaQuestCameraStream — streaming-only adapter for Quest 3 stereo cameras.

The Quest companion app exposes a 720p MJPEG stream per eye over HTTP
(``/preview/{left|right}``). This adapter:

* keeps one persistent MJPEG consumer per eye while connected (powering
  the viewer's video panel via the decoded ``latest_frame``);
* on :meth:`start_recording`, attaches a :class:`StreamingVideoRecorder`
  *sink* to each consumer so the same JPEG bytes are muxed straight
  into per-eye MP4 files on the Mac plus a sidecar timestamps JSONL —
  no Quest-side disk write, no end-of-session "pull" stage.

Why streaming-only:

* **No stop-time wait.** The previous design recorded MJPEG-AVI on the
  Quest then pulled ~90 MB per eye over HTTP after stop. On a 4G-class
  WiFi link that took 30 s+ and looked like a hang in the viewer.
* **Single channel.** Both viewer preview and recording read the same
  stream — fewer Quest-side resources, fewer failure modes.
* **Bit-exact quality.** Frames are muxed without re-encoding so what
  the viewer sees is exactly what ends up in the file.

See ``docs/superpowers/specs/2026-04-13-metaquest-stereo-camera-design.md``
for the broader protocol design.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional, Tuple

import httpx

from syncfield.adapters.meta_quest_camera.http_client import QuestHttpClient
from syncfield.adapters.meta_quest_camera.mp4_writer import (
    StreamingVideoRecorder,
    StreamingVideoResult,
)
from syncfield.adapters.meta_quest_camera.preview import (
    MjpegFrame,
    MjpegPreviewConsumer,
)
from syncfield.clock import SessionClock
from syncfield.stream import DeviceKey, StreamBase
from syncfield.types import (
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    StreamCapabilities,
)


logger = logging.getLogger(__name__)


# Matches the Quest companion Unity app's default HTTP port.
DEFAULT_QUEST_HTTP_PORT = 14045
DEFAULT_FPS = 30
# Quest 3 PCA's native sensor is 1280x960 (4:3). Asking ARFoundation
# to resize to a different aspect ratio (e.g. 1280x720) makes Meta's
# XRCpuImage.Convert silently produce no output — preview goes black.
# Match native to keep the encode path lossless and the conversion path
# trivial.
DEFAULT_RESOLUTION: Tuple[int, int] = (1280, 960)

# Multipart boundary string the Quest sender embeds in /preview/{eye}
# Content-Type. Lives here (not in the consumer) because it is part of
# the protocol contract between the two sides.
_PREVIEW_BOUNDARY = b"syncfield"


class MetaQuestCameraStream(StreamBase):
    """Stream + record Meta Quest 3 stereo passthrough cameras over HTTP.

    The Quest sender (``opengraph-studio/unity/SyncFieldQuest3Sender``)
    publishes a 720p MJPEG stream per eye on UDP / TCP port
    ``quest_port`` (default 14045). Connecting opens both streams and
    keeps them alive for the lifetime of the adapter; recording is then
    a cheap toggle that attaches a passthrough MP4 writer to each
    stream.

    Files written under ``output_dir`` per recorded session:

    ``{stream_id}_left.mp4``        MJPEG-in-MP4, no re-encode
    ``{stream_id}_right.mp4``       MJPEG-in-MP4, no re-encode
    ``{stream_id}_left.timestamps.jsonl``   per-frame host + quest-native ns
    ``{stream_id}_right.timestamps.jsonl``  per-frame host + quest-native ns
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

        # Per-recording state — non-None only between start_recording
        # and stop_recording.
        self._recorder_left: Optional[StreamingVideoRecorder] = None
        self._recorder_right: Optional[StreamingVideoRecorder] = None
        self._session_id: Optional[str] = None
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None
        self._frame_count = 0  # frames sample-emitted (left eye is authoritative)

    @property
    def device_key(self) -> Optional[DeviceKey]:
        return ("meta_quest_camera", self._quest_host)

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # 4-phase lifecycle
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        pass

    def connect(self) -> None:
        if self._connected:
            return
        self._http = QuestHttpClient(
            host=self._quest_host,
            port=self._quest_port,
            transport=self._transport,
        )
        # Probe reachability up front so a missing Quest fails the
        # whole connect() instead of silently flapping inside the
        # preview consumer's reconnect loop.
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
        # If a recording is somehow still active when disconnect() is
        # called, finalise it best-effort first so the MP4 trailer
        # gets written and the file is playable.
        if self._recorder_left is not None or self._recorder_right is not None:
            try:
                self.stop_recording()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[%s] disconnect-time stop_recording failed: %s", self.id, exc,
                )

        for consumer in (self._preview_left, self._preview_right):
            if consumer is not None:
                consumer.stop()
        self._preview_left = None
        self._preview_right = None
        if self._http is not None:
            self._http.close()
            self._http = None
        self._connected = False

    def start_recording(self, session_clock: SessionClock) -> None:
        if not self._connected:
            raise RuntimeError("start_recording() called before connect()")
        if self._recorder_left is not None or self._recorder_right is not None:
            raise RuntimeError("recording already in progress")

        self._session_id = (
            f"ep_{session_clock.sync_point.timestamp_ms}"
            f"_{session_clock.sync_point.host_id}"
        )
        self._frame_count = 0
        self._first_at = None
        self._last_at = None

        # Stand up one writer per eye. Both write under the orchestrator's
        # output_dir using the stream-id prefix, so the four artifacts of
        # this session sit next to each other in the episode folder.
        self._recorder_left = StreamingVideoRecorder(
            output_dir=self._output_dir,
            stream_id=self.id,
            side="left",
            fps=self._fps,
            width=self._resolution[0],
            height=self._resolution[1],
        )
        self._recorder_right = StreamingVideoRecorder(
            output_dir=self._output_dir,
            stream_id=self.id,
            side="right",
            fps=self._fps,
            width=self._resolution[0],
            height=self._resolution[1],
        )
        self._recorder_left.start()
        self._recorder_right.start()

        # Hot-attach sinks. The preview consumers stay running across
        # the recording cycle — viewer feed never blanks.
        assert self._preview_left is not None and self._preview_right is not None
        self._preview_left.set_frame_sink(self._make_sink(self._recorder_left, "left"))
        self._preview_right.set_frame_sink(self._make_sink(self._recorder_right, "right"))

        logger.info(
            "[%s] streaming recording started (session=%s, %dx%d @ %dfps)",
            self.id, self._session_id,
            self._resolution[0], self._resolution[1], self._fps,
        )

    def stop_recording(self) -> FinalizationReport:
        # Detach sinks first so no more frames land in a half-closed
        # writer while we flush.
        if self._preview_left is not None:
            self._preview_left.set_frame_sink(None)
        if self._preview_right is not None:
            self._preview_right.set_frame_sink(None)

        result_left = self._recorder_left.stop() if self._recorder_left else None
        result_right = self._recorder_right.stop() if self._recorder_right else None
        self._recorder_left = None
        self._recorder_right = None

        status, error = self._classify_outcome(result_left, result_right)
        return FinalizationReport(
            stream_id=self.id,
            status=status,
            frame_count=self._frame_count,
            file_path=result_left.output_path if result_left is not None else None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=error,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _classify_outcome(
        self,
        left: Optional[StreamingVideoResult],
        right: Optional[StreamingVideoResult],
    ) -> Tuple[str, Optional[str]]:
        """Map per-eye write results to (status, error_message)."""
        if left is None or right is None:
            return "failed", "recorder was not started"
        if left.frame_count == 0 and right.frame_count == 0:
            return "failed", "no frames received during recording"
        msgs = []
        if left.write_errors > 0 or right.write_errors > 0:
            msgs.append(
                f"mux errors left={left.write_errors} right={right.write_errors}"
            )
        if left.frame_count == 0 or right.frame_count == 0:
            msgs.append(
                f"single-eye recording: left={left.frame_count} right={right.frame_count}"
            )
        if msgs:
            return "partial", "; ".join(msgs)
        return "completed", None

    def _make_preview(self, side: str) -> MjpegPreviewConsumer:
        url = f"http://{self._quest_host}:{self._quest_port}/preview/{side}"
        return MjpegPreviewConsumer(
            url=url,
            boundary=_PREVIEW_BOUNDARY,
            transport=self._transport,
            decode_jpeg=True,
            on_health=self._make_health_callback(side),
        )

    def _make_health_callback(self, side: str):
        kind_map = {
            "drop": HealthEventKind.DROP,
            "reconnect": HealthEventKind.RECONNECT,
            "warning": HealthEventKind.WARNING,
        }

        def _on_health(kind: str, detail: str) -> None:
            self._emit_health(
                HealthEvent(
                    stream_id=self.id,
                    kind=kind_map.get(kind, HealthEventKind.WARNING),
                    at_ns=time.monotonic_ns(),
                    detail=f"[{side}] {detail}",
                )
            )

        return _on_health

    def _make_sink(self, recorder: StreamingVideoRecorder, side: str):
        """Build the per-frame sink the preview consumer calls.

        The sink does two things on each frame:

        1. Mux the JPEG into the per-eye MP4 + emit a timestamp line.
        2. Emit a SampleEvent through ``StreamBase`` so the orchestrator
           sees the recording progressing (viewer counter, sync stats).

        Only the LEFT-eye sink emits SampleEvents — emitting from both
        would double-count frames for what is, conceptually, one
        synchronised stereo capture per tick.
        """
        is_authoritative = side == "left"

        def _sink(frame: MjpegFrame) -> None:
            recorder.write_frame(
                frame.jpeg_bytes, frame.capture_ns, frame.quest_native_ns,
            )
            if not is_authoritative:
                return
            if self._first_at is None:
                self._first_at = frame.capture_ns
            self._last_at = frame.capture_ns
            frame_number = self._frame_count
            self._frame_count += 1
            self._emit_sample(SampleEvent(
                stream_id=self.id,
                frame_number=frame_number,
                capture_ns=frame.capture_ns,
                channels={},  # video stream has no scalar channels
                uncertainty_ns=self.UNCERTAINTY_NS,
                clock_domain=self.CLOCK_DOMAIN,
            ))

        return _sink

    # ------------------------------------------------------------------
    # Viewer-facing properties
    # ------------------------------------------------------------------

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
        """Single-eye preview for the viewer panel.

        Quest 3's ARFoundation only exposes the "primary" passthrough
        camera, so /preview/left and /preview/right currently carry
        identical pixels. Showing a side-by-side composite would waste
        space + bandwidth without conveying any extra information.
        Surface the left eye alone; we fall back to right if left
        hasn't produced its first decoded frame yet.

        True per-eye stereo would need either a Camera2 NDK wrapper or
        a full XR backend swap to OVR plugin (see PR #75 retrospective).
        Until then, one stream is the honest representation.
        """
        left = self.latest_frame_left
        if left is not None:
            return left
        return self.latest_frame_right
