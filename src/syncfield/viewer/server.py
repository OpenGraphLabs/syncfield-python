"""FastAPI server for the SyncField web viewer.

Endpoints:

- **WebSocket** ``/ws/control`` — 10 Hz snapshot broadcast + control commands
- **MJPEG** ``/stream/video/{stream_id}`` — continuous JPEG frames
- **SSE** ``/stream/sensor/{stream_id}`` — sensor channel push (~10 Hz)
- **REST** ``/api/status``, ``/api/discover``, ``/api/streams/{id}`` — one-shot queries
- **Episodes** ``/api/episodes``, ``/api/episodes/{id}``, ``/api/episodes/{id}/video/{file}`` — episode review
- **Sync** ``/api/episodes/{id}/sync``, ``/api/episodes/{id}/sync-status/{job_id}`` — sync orchestration
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
from starlette.responses import FileResponse, StreamingResponse

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
# Episode helpers
# ---------------------------------------------------------------------------


def _scan_episodes(data_dir: Path) -> List[Dict[str, Any]]:
    """Scan a directory for ``ep_*`` episode subdirectories.

    Parses the timestamp embedded in the directory name, checks for
    ``manifest.json`` and ``sync_report.json`` / ``synced/`` artefacts,
    and returns summaries sorted newest-first.
    """
    episodes: List[Dict[str, Any]] = []
    if not data_dir.is_dir():
        return episodes

    for entry in data_dir.iterdir():
        if not entry.is_dir() or not entry.name.startswith("ep_"):
            continue

        ep_id = entry.name
        manifest_path = entry / "manifest.json"
        has_manifest = manifest_path.exists()
        synced_dir = entry / "synced"
        sync_report_path = entry / "sync_report.json"
        if not sync_report_path.exists():
            sync_report_path = synced_dir / "sync_report.json"
        has_sync = sync_report_path.exists() or synced_dir.exists()

        # Parse created_at from directory name: ep_YYYYMMDD_HHMMSS_*
        created_at: Optional[str] = None
        parts = ep_id.split("_")
        if len(parts) >= 3:
            try:
                date_str = parts[1]  # YYYYMMDD
                time_str = parts[2]  # HHMMSS
                created_at = (
                    f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                    f"T{time_str[:2]}:{time_str[2:4]}:{time_str[4:6]}"
                )
            except (IndexError, ValueError):
                pass

        stream_count = 0
        host_id: Optional[str] = None
        if has_manifest:
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)
                host_id = manifest.get("host_id")
                stream_count = len(manifest.get("streams", {}))
            except Exception:
                pass

        episodes.append({
            "id": ep_id,
            "path": str(entry),
            "has_manifest": has_manifest,
            "has_sync": has_sync,
            "stream_count": stream_count,
            "host_id": host_id,
            "created_at": created_at,
        })

    # Sort newest first
    episodes.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    return episodes


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
        sync_endpoint: str = "http://localhost:8080",
    ) -> None:
        self._session = session
        self._poller = poller
        self._title = title
        self._sync_endpoint = sync_endpoint.rstrip("/")
        self._ws_clients: Set[WebSocket] = set()

        self.app = FastAPI(title=title, docs_url=None, redoc_url=None)
        self._setup_middleware()
        self._setup_routes()
        self._setup_episode_routes()
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
                import syncfield.adapters  # noqa: F401 — register discoverers
                from syncfield.discovery import scan

                report = await asyncio.to_thread(scan, use_cache=False)
                result = [
                    {
                        "id": d.device_id,
                        "name": d.display_name,
                        "adapter": d.adapter_type,
                        "kind": d.kind,
                        "description": d.description,
                        "in_use": d.in_use,
                        "warnings": list(d.warnings),
                    }
                    for d in report.devices
                ]
                errors = (
                    list(report.errors.values()) if report.errors else None
                )
                return JSONResponse({
                    "devices": result,
                    "error": errors[0] if errors else None,
                })
            except ImportError:
                return JSONResponse({"devices": [], "error": "discovery not available"})
            except Exception as exc:
                logger.exception("Discovery failed")
                return JSONResponse(
                    {"devices": [], "error": str(exc)}, status_code=500
                )

        @app.post("/api/streams/{stream_id}")
        async def api_add_stream(stream_id: str) -> JSONResponse:
            """Add a discovered device to the session by device_id."""
            try:
                import syncfield.adapters  # noqa: F401
                from syncfield.discovery import scan
                from syncfield.discovery._id_gen import make_stream_id

                report = await asyncio.to_thread(scan)
                device = next(
                    (d for d in report.devices if d.device_id == stream_id),
                    None,
                )
                if device is None:
                    return JSONResponse(
                        {"error": f"Device {stream_id!r} not found"},
                        status_code=404,
                    )
                sid = make_stream_id(device.display_name)
                kwargs: Dict[str, Any] = {"id": sid}
                if device.accepts_output_dir:
                    kwargs["output_dir"] = self._session.output_dir
                stream = device.construct(**kwargs)
                self._session.add(stream)
                return JSONResponse({"status": "added", "id": sid})
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
    # Episode review & sync routes
    # ------------------------------------------------------------------

    def _setup_episode_routes(self) -> None:
        app = self.app

        @app.get("/api/episodes")
        async def api_list_episodes() -> JSONResponse:
            """List all episodes in the data root directory."""
            try:
                data_dir = self._session.output_dir.parent
                episodes = await asyncio.to_thread(_scan_episodes, data_dir)
                return JSONResponse({"episodes": episodes})
            except Exception as exc:
                logger.exception("Failed to list episodes")
                return JSONResponse(
                    {"episodes": [], "error": str(exc)}, status_code=500,
                )

        @app.get("/api/episodes/{episode_id}")
        async def api_episode_detail(episode_id: str) -> JSONResponse:
            """Return episode manifest and sync report."""
            ep_dir = self._session.output_dir.parent / episode_id
            if not ep_dir.is_dir():
                return JSONResponse(
                    {"error": f"Episode {episode_id!r} not found"},
                    status_code=404,
                )
            try:
                manifest: Optional[Dict[str, Any]] = None
                manifest_path = ep_dir / "manifest.json"
                if manifest_path.exists():
                    raw = await asyncio.to_thread(manifest_path.read_text)
                    manifest = json.loads(raw)

                sync_report: Optional[Dict[str, Any]] = None
                for candidate in (
                    ep_dir / "sync_report.json",
                    ep_dir / "synced" / "sync_report.json",
                ):
                    if candidate.exists():
                        raw = await asyncio.to_thread(candidate.read_text)
                        sync_report = json.loads(raw)
                        break

                synced_dir = ep_dir / "synced"
                has_synced_videos = synced_dir.is_dir() and any(
                    f.suffix.lower() in {".mp4", ".mov", ".avi"}
                    for f in synced_dir.iterdir()
                    if f.is_file()
                )

                streams: List[str] = []
                if manifest and "streams" in manifest:
                    streams = list(manifest["streams"].keys())

                return JSONResponse({
                    "id": episode_id,
                    "manifest": manifest,
                    "sync_report": sync_report,
                    "has_synced_videos": has_synced_videos,
                    "streams": streams,
                })
            except Exception as exc:
                logger.exception("Failed to read episode %s", episode_id)
                return JSONResponse({"error": str(exc)}, status_code=500)

        @app.get("/api/episodes/{episode_id}/video/{filename:path}")
        async def api_episode_video(
            episode_id: str, filename: str,
        ) -> Any:
            """Serve a video file, preferring the synced version."""
            ep_dir = self._session.output_dir.parent / episode_id
            if not ep_dir.is_dir():
                return JSONResponse(
                    {"error": f"Episode {episode_id!r} not found"},
                    status_code=404,
                )

            # Prefer synced/ version
            synced_path = ep_dir / "synced" / filename
            root_path = ep_dir / filename

            file_path: Optional[Path] = None
            if synced_path.is_file():
                file_path = synced_path
            elif root_path.is_file():
                file_path = root_path

            if file_path is None:
                return JSONResponse(
                    {"error": f"Video file {filename!r} not found"},
                    status_code=404,
                )

            ext = file_path.suffix.lower()
            media_types = {
                ".mp4": "video/mp4",
                ".mov": "video/quicktime",
                ".avi": "video/x-msvideo",
            }
            media_type = media_types.get(ext, "application/octet-stream")
            return FileResponse(str(file_path), media_type=media_type)

        @app.post("/api/episodes/{episode_id}/sync")
        async def api_trigger_sync(episode_id: str) -> JSONResponse:
            """Trigger sync via Docker container using multipart upload.

            Uses the ``/sync/upload`` endpoint so the Docker container
            doesn't need volume access to the host filesystem.
            """
            ep_dir = self._session.output_dir.parent / episode_id
            if not ep_dir.is_dir():
                return JSONResponse(
                    {"error": f"Episode {episode_id!r} not found"},
                    status_code=404,
                )

            manifest_path = ep_dir / "manifest.json"
            if not manifest_path.exists():
                return JSONResponse(
                    {"error": "manifest.json not found"}, status_code=400,
                )

            try:
                raw = await asyncio.to_thread(manifest_path.read_text)
                manifest = json.loads(raw)
            except Exception as exc:
                return JSONResponse(
                    {"error": f"Failed to read manifest: {exc}"},
                    status_code=500,
                )

            streams_cfg = manifest.get("streams", {})

            # Collect files and metadata for the multipart upload
            stream_ids: List[str] = []
            file_paths: List[Path] = []
            timestamp_paths: List[Path] = []
            primary_id: Optional[str] = None

            for stream_id, meta in streams_cfg.items():
                kind = meta.get("kind", "video")

                # Find the data file
                if kind == "video":
                    file_path = Path(meta["path"]) if "path" in meta else ep_dir / f"{stream_id}.mp4"
                elif kind == "sensor":
                    file_path = Path(meta["path"]) if "path" in meta else ep_dir / f"{stream_id}.jsonl"
                else:
                    continue

                if not file_path.exists():
                    continue

                stream_ids.append(stream_id)
                file_paths.append(file_path)

                # Primary = first video stream
                if kind == "video" and primary_id is None:
                    primary_id = stream_id

                # Timestamps file
                ts_path = ep_dir / f"{stream_id}.timestamps.jsonl"
                if ts_path.exists():
                    timestamp_paths.append(ts_path)

            if len(file_paths) < 2:
                return JSONResponse(
                    {"error": "At least 2 streams required for sync"},
                    status_code=400,
                )

            url = f"{self._sync_endpoint}/api/v1/sync/upload"

            try:
                def _do_upload() -> dict:
                    import urllib.request
                    import urllib.error

                    boundary = f"----SyncFieldBoundary{id(file_paths)}"
                    body_parts: List[bytes] = []

                    # Stream IDs field
                    body_parts.append(
                        f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="stream_ids"\r\n\r\n'
                        f"{','.join(stream_ids)}\r\n".encode()
                    )

                    # Primary ID field
                    if primary_id:
                        body_parts.append(
                            f"--{boundary}\r\n"
                            f'Content-Disposition: form-data; name="primary_id"\r\n\r\n'
                            f"{primary_id}\r\n".encode()
                        )

                    # Config fields
                    for name, value in [
                        ("confidence_threshold", "0.4"),
                        ("reencode", "true"),
                        ("aggregation", "nearest"),
                    ]:
                        body_parts.append(
                            f"--{boundary}\r\n"
                            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                            f"{value}\r\n".encode()
                        )

                    # Data files
                    for fp in file_paths:
                        body_parts.append(
                            f"--{boundary}\r\n"
                            f'Content-Disposition: form-data; name="files"; '
                            f'filename="{fp.name}"\r\n'
                            f"Content-Type: application/octet-stream\r\n\r\n".encode()
                        )
                        body_parts.append(fp.read_bytes())
                        body_parts.append(b"\r\n")

                    # Timestamp files
                    for tp in timestamp_paths:
                        body_parts.append(
                            f"--{boundary}\r\n"
                            f'Content-Disposition: form-data; name="timestamp_files"; '
                            f'filename="{tp.name}"\r\n'
                            f"Content-Type: application/octet-stream\r\n\r\n".encode()
                        )
                        body_parts.append(tp.read_bytes())
                        body_parts.append(b"\r\n")

                    body_parts.append(f"--{boundary}--\r\n".encode())
                    body = b"".join(body_parts)

                    req = urllib.request.Request(
                        url,
                        data=body,
                        headers={
                            "Content-Type": f"multipart/form-data; boundary={boundary}",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        return json.loads(resp.read().decode("utf-8"))

                result = await asyncio.to_thread(_do_upload)
                return JSONResponse(result)
            except Exception as exc:
                detail = str(exc)
                if hasattr(exc, "read"):
                    detail = exc.read().decode("utf-8", errors="replace")  # type: ignore[union-attr]
                logger.exception("Sync upload failed")
                return JSONResponse(
                    {"error": f"Failed to trigger sync ({detail})"},
                    status_code=502,
                )

        @app.get("/api/episodes/{episode_id}/sync-status/{job_id}")
        async def api_sync_status(
            episode_id: str, job_id: str,
        ) -> JSONResponse:
            """Proxy sync job status and download results when complete."""
            import urllib.request
            import urllib.error

            url = f"{self._sync_endpoint}/api/v1/jobs/{job_id}"
            try:
                req = urllib.request.Request(url, method="GET")

                def _do_get():
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        return json.loads(resp.read().decode("utf-8"))

                result = await asyncio.to_thread(_do_get)

                # When sync is complete, download results into episode dir
                if result.get("status") == "complete":
                    ep_dir = self._session.output_dir.parent / episode_id
                    await asyncio.to_thread(
                        _download_sync_results,
                        self._sync_endpoint, job_id, result, ep_dir,
                    )

                return JSONResponse(result)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                return JSONResponse(
                    {"error": f"Sync service returned {exc.code}", "detail": body},
                    status_code=502,
                )
            except Exception as exc:
                logger.exception("Failed to poll sync status")
                return JSONResponse({"error": str(exc)}, status_code=502)

        @app.get("/api/episodes/{episode_id}/frame-map")
        async def api_frame_map(episode_id: str) -> JSONResponse:
            """Parse and return frame_map.jsonl for the episode."""
            ep_dir = self._session.output_dir.parent / episode_id
            if not ep_dir.is_dir():
                return JSONResponse(
                    {"error": f"Episode {episode_id!r} not found"},
                    status_code=404,
                )

            frame_map_path: Optional[Path] = None
            for candidate in (
                ep_dir / "frame_map.jsonl",
                ep_dir / "synced" / "frame_map.jsonl",
            ):
                if candidate.exists():
                    frame_map_path = candidate
                    break

            if frame_map_path is None:
                return JSONResponse({"frames": [], "error": "not available"})

            try:
                raw = await asyncio.to_thread(frame_map_path.read_text)
                frames: List[Dict[str, Any]] = []
                for line in raw.strip().splitlines():
                    line = line.strip()
                    if line:
                        frames.append(json.loads(line))
                return JSONResponse({"frames": frames})
            except Exception as exc:
                logger.exception("Failed to read frame_map.jsonl")
                return JSONResponse(
                    {"frames": [], "error": str(exc)}, status_code=500,
                )

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


def _download_sync_results(
    sync_endpoint: str, job_id: str, result: dict, ep_dir: Path,
) -> None:
    """Download sync output files into the episode directory.

    Called when sync status reaches ``complete``. Downloads
    ``sync_report.json``, ``frame_map.jsonl``, and synced videos
    into ``ep_dir/synced/``.
    """
    import urllib.request

    synced_dir = ep_dir / "synced"
    synced_dir.mkdir(exist_ok=True)

    sync_result = result.get("result", {})
    files_to_download: List[str] = []

    # Always try sync_report.json and frame_map.jsonl
    if sync_result.get("sync_report"):
        files_to_download.append(sync_result["sync_report"])
    if sync_result.get("frame_map"):
        files_to_download.append(sync_result["frame_map"])
    # Synced videos
    for vid in sync_result.get("synced_videos", []):
        files_to_download.append(vid)

    for filepath in files_to_download:
        try:
            url = f"{sync_endpoint}/api/v1/jobs/{job_id}/download/{filepath}"
            local_path = ep_dir / filepath
            local_path.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(url, str(local_path))
            logger.info("Downloaded sync result: %s", local_path)
        except Exception:
            logger.warning("Failed to download sync file: %s", filepath)


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
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
    }
    return types.get(ext, "application/octet-stream")
