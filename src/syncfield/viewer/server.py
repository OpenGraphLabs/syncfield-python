"""FastAPI server for the SyncField web viewer.

Endpoints:

- **WebSocket** ``/ws/control`` — 10 Hz snapshot broadcast + control commands
- **MJPEG** ``/stream/video/{stream_id}`` — continuous JPEG frames
- **SSE** ``/stream/sensor/{stream_id}`` — sensor channel push (~10 Hz)
- **REST** ``/api/status``, ``/api/discover``, ``/api/streams/{id}`` — one-shot queries
- **Static** ``/*`` — built React SPA (production) or proxied Vite dev server

The server holds a direct reference to the :class:`SessionOrchestrator` and
its :class:`SessionPoller` — no IPC, no serialization overhead. Video frames
and sensor data flow through dedicated streaming channels so the WebSocket
payload stays lightweight.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from syncfield.orchestrator import SessionOrchestrator
from syncfield.viewer.poller import SessionPoller
from syncfield.viewer.state import HealthEntry, SessionSnapshot, StreamSnapshot

logger = logging.getLogger(__name__)

# Directory containing the built React app (vite build output).
STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Snapshot serialization
# ---------------------------------------------------------------------------


def snapshot_to_dict(snapshot: SessionSnapshot) -> Dict[str, Any]:
    """Convert a frozen SessionSnapshot to a JSON-serializable dict.

    Excludes ``latest_frame`` (numpy array) and ``plot_points`` (deque) —
    those are streamed over dedicated MJPEG/SSE channels.
    """
    now_ns = time.monotonic_ns()

    streams: Dict[str, Any] = {}
    for sid, s in snapshot.streams.items():
        last_sample_ms_ago: Optional[float] = None
        if s.last_sample_at_ns is not None:
            last_sample_ms_ago = round((now_ns - s.last_sample_at_ns) / 1e6, 1)

        streams[sid] = {
            "id": s.id,
            "kind": s.kind,
            "frame_count": s.frame_count,
            "effective_hz": round(s.effective_hz, 1),
            "last_sample_ms_ago": last_sample_ms_ago,
            "provides_audio_track": s.provides_audio_track,
            "produces_file": s.produces_file,
            "health_count": s.health_count,
        }

    health_log: List[Dict[str, Any]] = []
    for h in snapshot.health_log:
        at_s = round(h.at_ns / 1e9, 3) if h.at_ns else 0
        health_log.append({
            "stream_id": h.stream_id,
            "kind": h.kind,
            "at_s": at_s,
            "detail": h.detail,
        })

    return {
        "type": "snapshot",
        "state": snapshot.state,
        "host_id": snapshot.host_id,
        "elapsed_s": round(snapshot.elapsed_s, 3),
        "chirp": {
            "enabled": snapshot.chirp_enabled,
            "start_ns": snapshot.chirp_start_ns,
            "stop_ns": snapshot.chirp_stop_ns,
        },
        "streams": streams,
        "health_log": health_log,
        "output_dir": snapshot.output_dir,
    }


# ---------------------------------------------------------------------------
# Server class
# ---------------------------------------------------------------------------


class ViewerServer:
    """FastAPI application for the SyncField web viewer.

    Owns the FastAPI app instance and wires all routes. The caller
    (:class:`ViewerApp`) provides the session and poller references.
    """

    def __init__(
        self,
        session: SessionOrchestrator,
        poller: SessionPoller,
        *,
        title: str = "SyncField",
    ) -> None:
        self._session = session
        self._poller = poller
        self._title = title
        self._ws_clients: Set[WebSocket] = set()

        self.app = FastAPI(title=title, docs_url=None, redoc_url=None)
        self._setup_middleware()
        self._setup_routes()
        self._setup_static()

    # ------------------------------------------------------------------
    # Middleware
    # ------------------------------------------------------------------

    def _setup_middleware(self) -> None:
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    def _setup_routes(self) -> None:
        app = self.app

        # -- WebSocket: snapshot broadcast + control commands ---------------

        @app.websocket("/ws/control")
        async def ws_control(ws: WebSocket) -> None:
            await ws.accept()
            self._ws_clients.add(ws)
            try:
                # Start background task to broadcast snapshots
                broadcast_task = asyncio.create_task(
                    self._broadcast_loop(ws)
                )
                # Listen for control commands
                while True:
                    data = await ws.receive_text()
                    await self._handle_command(data)
            except WebSocketDisconnect:
                pass
            except Exception:
                logger.debug("WebSocket connection closed")
            finally:
                self._ws_clients.discard(ws)
                broadcast_task.cancel()
                try:
                    await broadcast_task
                except asyncio.CancelledError:
                    pass

        # -- MJPEG: continuous video frames ---------------------------------

        @app.get("/stream/video/{stream_id}")
        async def stream_video(stream_id: str) -> StreamingResponse:
            return StreamingResponse(
                self._mjpeg_generator(stream_id),
                media_type="multipart/x-mixed-replace; boundary=frame",
            )

        # -- SSE: sensor channel data push ----------------------------------

        @app.get("/stream/sensor/{stream_id}")
        async def stream_sensor(stream_id: str) -> StreamingResponse:
            return StreamingResponse(
                self._sse_generator(stream_id),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # -- REST: one-shot queries -----------------------------------------

        @app.get("/api/status")
        async def api_status() -> JSONResponse:
            snapshot = self._poller.get_snapshot()
            if snapshot is None:
                return JSONResponse({"state": "initializing"})
            return JSONResponse(snapshot_to_dict(snapshot))

        @app.post("/api/discover")
        async def api_discover() -> JSONResponse:
            """Trigger device discovery scan."""
            try:
                from syncfield.discovery import discover_devices
                devices = await asyncio.to_thread(discover_devices)
                result = [
                    {
                        "id": d.id,
                        "name": d.name,
                        "adapter": d.adapter,
                        "kind": d.kind,
                    }
                    for d in devices
                ]
                return JSONResponse({"devices": result})
            except ImportError:
                return JSONResponse({"devices": [], "error": "discovery not available"})
            except Exception as exc:
                logger.exception("Discovery failed")
                return JSONResponse(
                    {"devices": [], "error": str(exc)}, status_code=500
                )

        @app.post("/api/streams/{stream_id}")
        async def api_add_stream(stream_id: str) -> JSONResponse:
            """Add a discovered device to the session."""
            try:
                from syncfield.discovery import discover_devices, build_stream
                devices = await asyncio.to_thread(discover_devices)
                device = next((d for d in devices if d.id == stream_id), None)
                if device is None:
                    return JSONResponse(
                        {"error": f"Device {stream_id!r} not found"},
                        status_code=404,
                    )
                stream = build_stream(device)
                self._session.add(stream)
                return JSONResponse({"status": "added", "id": stream_id})
            except Exception as exc:
                logger.exception("Failed to add stream")
                return JSONResponse({"error": str(exc)}, status_code=500)

        @app.delete("/api/streams/{stream_id}")
        async def api_remove_stream(stream_id: str) -> JSONResponse:
            """Remove a stream from the session."""
            try:
                self._session.remove(stream_id)
                return JSONResponse({"status": "removed", "id": stream_id})
            except Exception as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

    # ------------------------------------------------------------------
    # Static files (built React app)
    # ------------------------------------------------------------------

    def _setup_static(self) -> None:
        if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
            # Serve the SPA — catch-all returns index.html for client-side routing
            @self.app.get("/{full_path:path}")
            async def spa_fallback(full_path: str) -> HTMLResponse:
                # Try to serve the exact file first
                file_path = STATIC_DIR / full_path
                if full_path and file_path.exists() and file_path.is_file():
                    content = file_path.read_bytes()
                    media_type = _guess_media_type(full_path)
                    return HTMLResponse(content=content, media_type=media_type)
                # Fallback to index.html for SPA routing
                return HTMLResponse(content=(STATIC_DIR / "index.html").read_text())

    # ------------------------------------------------------------------
    # WebSocket broadcast loop
    # ------------------------------------------------------------------

    async def _broadcast_loop(self, ws: WebSocket) -> None:
        """Send snapshot JSON to a single WebSocket client at ~10 Hz."""
        while True:
            snapshot = self._poller.get_snapshot()
            if snapshot is not None:
                try:
                    payload = snapshot_to_dict(snapshot)
                    await ws.send_text(json.dumps(payload))
                except Exception:
                    break
            await asyncio.sleep(0.1)

    async def broadcast_countdown(self, count: int) -> None:
        """Send a countdown event to all connected WebSocket clients."""
        message = json.dumps({"type": "countdown", "count": count})
        disconnected: List[WebSocket] = []
        for ws in self._ws_clients:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            self._ws_clients.discard(ws)

    # ------------------------------------------------------------------
    # Command handler
    # ------------------------------------------------------------------

    async def _handle_command(self, raw: str) -> None:
        """Process a control command from a WebSocket client."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid WebSocket message: %s", raw)
            return

        action = msg.get("action")
        if action == "connect":
            await asyncio.to_thread(self._session.connect)
        elif action == "disconnect":
            await asyncio.to_thread(self._session.disconnect)
        elif action == "record":
            countdown_s = msg.get("countdown_s", 3)
            await self._start_recording(countdown_s)
        elif action == "stop":
            await asyncio.to_thread(self._session.stop)
        elif action == "cancel":
            await asyncio.to_thread(self._session.cancel)
        else:
            logger.warning("Unknown action: %s", action)

    async def _start_recording(self, countdown_s: int) -> None:
        """Start recording via the session's native countdown.

        The session's ``start()`` owns the countdown timer, audio ticks,
        and chirp playback.  We pass ``on_countdown_tick`` so each tick
        also broadcasts a WebSocket event for the browser overlay — but
        no audio is played from the browser side.
        """
        loop = asyncio.get_event_loop()

        def _on_tick(n: int) -> None:
            # Schedule the broadcast on the event loop from the
            # session's calling thread.
            asyncio.run_coroutine_threadsafe(
                self.broadcast_countdown(n), loop,
            )

        await asyncio.to_thread(
            self._session.start,
            countdown_s=countdown_s,
            on_countdown_tick=_on_tick,
        )

    # ------------------------------------------------------------------
    # MJPEG generator
    # ------------------------------------------------------------------

    async def _mjpeg_generator(self, stream_id: str):
        """Yield JPEG frames as a multipart stream."""
        while True:
            snapshot = self._poller.get_snapshot()
            if snapshot is not None:
                stream = snapshot.streams.get(stream_id)
                if stream is not None and stream.latest_frame is not None:
                    try:
                        _, jpeg = cv2.imencode(
                            ".jpg", stream.latest_frame,
                            [cv2.IMWRITE_JPEG_QUALITY, 80],
                        )
                        frame_bytes = jpeg.tobytes()
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(frame_bytes)).encode() + b"\r\n"
                            b"\r\n" + frame_bytes + b"\r\n"
                        )
                    except Exception:
                        pass
            await asyncio.sleep(1 / 30)  # ~30 fps cap

    # ------------------------------------------------------------------
    # SSE generator
    # ------------------------------------------------------------------

    async def _sse_generator(self, stream_id: str):
        """Yield sensor data as Server-Sent Events."""
        while True:
            snapshot = self._poller.get_snapshot()
            if snapshot is not None:
                stream = snapshot.streams.get(stream_id)
                if stream is not None and stream.plot_points:
                    channels: Dict[str, float] = {}
                    label: Optional[float] = None
                    for ch_name, (xs, ys) in stream.plot_points.items():
                        if ys:
                            channels[ch_name] = ys[-1]
                        if xs and label is None:
                            label = xs[-1]

                    if channels:
                        event_data = json.dumps({
                            "channels": channels,
                            "label": label,
                        })
                        yield f"data: {event_data}\n\n"
            await asyncio.sleep(0.1)  # ~10 Hz


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _guess_media_type(path: str) -> str:
    """Guess MIME type from file extension for static file serving."""
    ext = Path(path).suffix.lower()
    types = {
        ".html": "text/html",
        ".js": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }
    return types.get(ext, "application/octet-stream")
