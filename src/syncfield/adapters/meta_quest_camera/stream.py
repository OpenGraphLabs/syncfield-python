"""MetaQuestCameraStream — single-eye streaming adapter for Quest 3 PCA.

The Quest companion app exposes the primary passthrough camera as a
1280×960 MJPEG stream over HTTP (``/preview/left``). This adapter:

* keeps one persistent MJPEG consumer alive while connected (powering
  the viewer's video panel via the decoded ``latest_frame``);
* on :meth:`start_recording`, attaches a :class:`StreamingVideoRecorder`
  *sink* to the consumer so the same JPEG bytes are muxed straight into
  ``{stream_id}.mp4`` on the Mac — no Quest-side disk write, no
  end-of-session pull;
* on :meth:`connect`, POSTs the host's monotonic-ns to Quest's
  ``/clock/sync`` endpoint so subsequent frame timestamps are projected
  into the Mac clock domain (otherwise jsonl ``capture_ns`` would carry
  raw Quest uptime, breaking cross-modal sync).

Why single-eye:

ARFoundation's ``cameraManager.TryAcquireLatestCpuImage`` only exposes
the "primary" passthrough camera — there's no per-eye selector. The
old design ran two MJPEG consumers + recorders against the same source
camera and produced bit-different but content-identical files
(~30 MB / minute wasted). True per-eye stereo would need either a
Camera2 NDK wrapper or an XR-backend swap to OVR plugin (see PR #75
retrospective). Until then, one stream is the honest representation.

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
# Quest 3 PCA's native sensor is 1280×960 (4:3). Asking ARFoundation to
# resize to a different aspect ratio (e.g. 1280×720) makes Meta's
# XRCpuImage.Convert silently produce no output — preview goes black.
DEFAULT_RESOLUTION: Tuple[int, int] = (1280, 960)

# We pull from /preview/left only. /preview/right serves identical
# bytes from the same primary camera — recording it would just waste
# disk space and bandwidth.
_PRIMARY_EYE = "left"

# Multipart boundary string the Quest sender embeds in /preview/{eye}
# Content-Type. Lives here (not in the consumer) because it is part of
# the protocol contract between the two sides.
_PREVIEW_BOUNDARY = b"syncfield"


class MetaQuestCameraStream(StreamBase):
    """Stream + record the Quest 3 primary passthrough camera over HTTP.

    Files written under ``output_dir`` per recorded session:

    ``{stream_id}.mp4``  — MJPEG-in-MP4, no re-encode

    The orchestrator additionally writes
    ``{stream_id}.timestamps.jsonl`` from the per-frame
    :class:`SampleEvent` stream, which carries both ``capture_ns``
    (host-projected monotonic ns) and ``quest_native_ns`` (raw Quest
    uptime in ns, exposed as a scalar channel for post-hoc clock-drift
    correction).
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
                target_hz=float(fps),
            ),
        )
        self._quest_host = quest_host
        self._quest_port = quest_port
        self._fps = fps
        self._resolution = resolution
        self._output_dir = Path(output_dir)
        self._transport = _transport

        self._http: Optional[QuestHttpClient] = None
        self._preview: Optional[MjpegPreviewConsumer] = None
        self._connected = False

        # Per-recording state — non-None only between start_recording
        # and stop_recording.
        self._recorder: Optional[StreamingVideoRecorder] = None
        self._session_id: Optional[str] = None
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None
        self._frame_count = 0  # frames sample-emitted

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
        # Probe reachability up front so a missing Quest fails the whole
        # connect() instead of silently flapping inside the preview
        # consumer's reconnect loop.
        self._http.status()

        # Calibrate the host↔Quest monotonic offset BEFORE the consumer
        # starts pulling frames. The very first JPEG header that arrives
        # will then carry an X-Frame-Capture-Ns already in the host
        # clock domain, so the orchestrator's jsonl is consistent from
        # frame 0.
        self._push_clock_sync()

        self._preview = self._make_preview(_PRIMARY_EYE)
        self._preview.start()
        self._connected = True
        logger.info(
            "[%s] connected to Quest %s:%d (single-eye streaming)",
            self.id, self._quest_host, self._quest_port,
        )

    def disconnect(self) -> None:
        # If a recording is somehow still active when disconnect() is
        # called, finalise it best-effort first so the MP4 trailer
        # gets written and the file is playable.
        if self._recorder is not None:
            try:
                self.stop_recording()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[%s] disconnect-time stop_recording failed: %s", self.id, exc,
                )

        if self._preview is not None:
            self._preview.stop()
            self._preview = None
        if self._http is not None:
            self._http.close()
            self._http = None
        self._connected = False

    def start_recording(self, session_clock: SessionClock) -> None:
        if not self._connected:
            raise RuntimeError("start_recording() called before connect()")
        if self._recorder is not None:
            raise RuntimeError("recording already in progress")

        self._session_id = (
            f"ep_{session_clock.sync_point.timestamp_ms}"
            f"_{session_clock.sync_point.host_id}"
        )
        self._frame_count = 0
        self._first_at = None
        self._last_at = None

        self._recorder = StreamingVideoRecorder(
            output_dir=self._output_dir,
            stream_id=self.id,
            fps=self._fps,
            width=self._resolution[0],
            height=self._resolution[1],
        )
        self._recorder.start()

        # Hot-attach the sink. The preview consumer keeps running across
        # the recording cycle so the viewer feed never blanks.
        assert self._preview is not None
        self._preview.set_frame_sink(self._make_sink(self._recorder))

        logger.info(
            "[%s] streaming recording started (session=%s, %dx%d @ %dfps)",
            self.id, self._session_id,
            self._resolution[0], self._resolution[1], self._fps,
        )

    def stop_recording(self) -> FinalizationReport:
        # Detach sink first so no more frames land in a half-closed
        # writer while we flush.
        if self._preview is not None:
            self._preview.set_frame_sink(None)

        result = self._recorder.stop() if self._recorder else None
        self._recorder = None

        status, error = self._classify_outcome(result)
        return FinalizationReport(
            stream_id=self.id,
            status=status,
            frame_count=self._frame_count,
            file_path=result.output_path if result is not None else None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=error,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _push_clock_sync(self) -> None:
        """POST host's monotonic ns to Quest so deltaNs is set server-side.

        Without this hook Quest's _deltaNs stays at zero and every
        X-Frame-Capture-Ns header carries raw Quest uptime instead of
        the host clock projection — cross-modal sync (audio, IMUs)
        breaks because timestamps are in the wrong domain.
        """
        if self._http is None:
            return
        host_mono_ns = time.monotonic_ns()
        self._http.clock_sync(host_mono_ns)
        logger.info(
            "[%s] Pushed clock sync host_mono_ns=%d to Quest",
            self.id, host_mono_ns,
        )

    def _classify_outcome(
        self, result: Optional[StreamingVideoResult],
    ) -> Tuple[str, Optional[str]]:
        if result is None:
            return "failed", "recorder was not started"
        if result.frame_count == 0:
            return "failed", "no frames received during recording"
        if result.write_errors > 0:
            return "partial", f"{result.write_errors} mux errors"
        return "completed", None

    def _make_preview(self, side: str) -> MjpegPreviewConsumer:
        url = f"http://{self._quest_host}:{self._quest_port}/preview/{side}"
        return MjpegPreviewConsumer(
            url=url,
            boundary=_PREVIEW_BOUNDARY,
            transport=self._transport,
            decode_jpeg=True,
            on_health=self._make_health_callback(),
        )

    def _make_health_callback(self):
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
                    detail=detail,
                )
            )

        return _on_health

    def _make_sink(self, recorder: StreamingVideoRecorder):
        """Build the per-frame sink the preview consumer calls.

        The sink does two things on each frame:

        1. Mux the JPEG into the MP4 (no re-encode).
        2. Emit a SampleEvent so the orchestrator's auto-jsonl captures
           the per-frame timestamps. ``quest_native_ns`` rides in
           ``channels`` so post-hoc tooling can correct host↔Quest
           clock drift over a long session.
        """

        def _sink(frame: MjpegFrame) -> None:
            recorder.write_frame(
                frame.jpeg_bytes, frame.capture_ns, frame.quest_native_ns,
            )
            if self._first_at is None:
                self._first_at = frame.capture_ns
            self._last_at = frame.capture_ns
            frame_number = self._frame_count
            self._frame_count += 1

            channels: dict = {}
            if frame.quest_native_ns is not None:
                # Surface as a scalar channel so the orchestrator's
                # jsonl writer naturally persists it under "channels".
                channels["quest_native_ns"] = float(frame.quest_native_ns)

            self._emit_sample(SampleEvent(
                stream_id=self.id,
                frame_number=frame_number,
                capture_ns=frame.capture_ns,
                channels=channels,
                uncertainty_ns=self.UNCERTAINTY_NS,
                clock_domain=self.CLOCK_DOMAIN,
            ))

        return _sink

    # ------------------------------------------------------------------
    # Viewer-facing properties
    # ------------------------------------------------------------------

    @property
    def latest_frame(self):
        """Most-recent decoded BGR preview frame, or None."""
        if self._preview is None:
            return None
        return self._preview.latest_frame
