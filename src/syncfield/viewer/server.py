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
import io
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from PIL import Image
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, StreamingResponse

from syncfield.orchestrator import SessionOrchestrator
from syncfield.viewer.poller import SessionPoller
from syncfield.viewer.state import AggregationSnapshot, SessionSnapshot, StreamSnapshot

logger = logging.getLogger(__name__)

# Directory containing the built React app (vite build output).
STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Snapshot serialization
# ---------------------------------------------------------------------------


def _serialize_aggregation(agg) -> Dict[str, Any]:
    """Serialize an AggregationSnapshot (or None) to a JSON-serializable dict."""
    if agg is None:
        return {"active_job": None, "queue_length": 0, "recent_jobs": []}
    return {
        "active_job": agg.active_job.to_dict() if agg.active_job else None,
        "queue_length": getattr(agg, "queue_length", 0),
        "recent_jobs": [j.to_dict() for j in getattr(agg, "recent_jobs", [])],
    }


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
            "capabilities": {
                "live_preview": getattr(s, "live_preview", True),
                "provides_audio_track": s.provides_audio_track,
                "produces_file": s.produces_file,
                "supports_precise_timestamps": getattr(s, "supports_precise_timestamps", False),
                "is_removable": getattr(s, "is_removable", False),
            },
        }

    def _serialize_incident(inc) -> Dict[str, Any]:
        return {
            "id": inc.id,
            "stream_id": inc.stream_id,
            "fingerprint": inc.fingerprint,
            "title": inc.title,
            "severity": inc.severity,
            "source": inc.source,
            "opened_at_ns": inc.opened_at_ns,
            "closed_at_ns": inc.closed_at_ns,
            "event_count": inc.event_count,
            "detail": inc.detail,
            "ago_s": round(inc.ago_s, 1),
            "artifacts": inc.artifacts,
        }

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
        "active_incidents": [_serialize_incident(i) for i in snapshot.active_incidents],
        "resolved_incidents": [_serialize_incident(i) for i in snapshot.resolved_incidents],
        "output_dir": snapshot.output_dir,
        "aggregation": _serialize_aggregation(getattr(snapshot, "aggregation", None)),
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
        task: Optional[str] = None
        if has_manifest:
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)
                host_id = manifest.get("host_id")
                stream_count = len(manifest.get("streams", {}))
                task = manifest.get("task")
            except Exception:
                pass

        episodes.append({
            "id": ep_id,
            "path": str(entry),
            "has_manifest": has_manifest,
            "has_sync": has_sync,
            "stream_count": stream_count,
            "host_id": host_id,
            "task": task,
            "created_at": created_at,
        })

    # Sort newest first
    episodes.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    return episodes


# ---------------------------------------------------------------------------
# Multi-host helpers
# ---------------------------------------------------------------------------


def _require_multihost(orch) -> None:
    """Raise HTTP 409 unless the orchestrator has an attached multi-host role."""
    if orch._role is None:
        raise HTTPException(
            status_code=409, detail="multi-host role not configured"
        )


def _require_leader(orch) -> None:
    """Raise HTTP 409 unless the orchestrator is running as a LeaderRole."""
    from syncfield.roles import LeaderRole

    _require_multihost(orch)
    if not isinstance(orch._role, LeaderRole):
        raise HTTPException(
            status_code=409,
            detail="this operation requires a LeaderRole",
        )


# ---------------------------------------------------------------------------
# Aggregation listener wiring
# ---------------------------------------------------------------------------

def _attach_aggregation_listener(server: "ViewerServer") -> None:
    """Best-effort: subscribe to the global Go3S aggregation queue.

    No-op if the Go3S camera extra is not installed.  Updates
    ``server._agg_state`` so the broadcast loop can inject the latest
    aggregation state into each WS snapshot without requiring a poller
    change.
    """
    try:
        from syncfield.adapters.insta360_go3s.stream import _global_aggregation_queue
        from syncfield.adapters.insta360_go3s.aggregation.types import AggregationState
    except ImportError:
        return

    def on_progress(progress) -> None:
        # RUNNING and FAILED both surface to the status bar so the user
        # sees in-flight progress and errors (with a Retry button).
        # COMPLETED clears the bar and is archived to recent_jobs for the
        # episode-list badges.
        if progress.state in (AggregationState.RUNNING, AggregationState.FAILED):
            active = progress
        else:
            active = None

        if progress.state in (AggregationState.COMPLETED, AggregationState.FAILED):
            with server._agg_lock:
                server._recent_agg_jobs.append(progress)
                if len(server._recent_agg_jobs) > 5:
                    del server._recent_agg_jobs[: len(server._recent_agg_jobs) - 5]

        with server._agg_lock:
            recent = list(server._recent_agg_jobs)
        server._agg_state = AggregationSnapshot(
            active_job=active,
            queue_length=0,  # populated by queue if exposed; staying 0 in v1
            recent_jobs=recent,
        )

    try:
        _global_aggregation_queue().subscribe(on_progress)
    except Exception:
        logger.warning(
            "Failed to subscribe to global aggregation queue; "
            "aggregation state will not be surfaced in the WS snapshot.",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Aggregation control command dispatcher
# ---------------------------------------------------------------------------


def _next_default_id(orchestrator: "SessionOrchestrator", prefix: str) -> str:
    """Return the next available stream id ``<prefix>_N`` not already in use."""
    existing = {s.id for s in orchestrator._streams.values()}
    n = 1
    while f"{prefix}_{n}" in existing:
        n += 1
    return f"{prefix}_{n}"


def handle_control_command(orchestrator: "SessionOrchestrator", payload: dict) -> dict:
    """Dispatch an aggregation control command from a WebSocket client.

    Handles the three aggregation commands introduced in T14, plus the T17
    ``add_go3s_stream`` command.  All other (legacy) commands continue to be
    handled inside ``_handle_command``.

    Returns a ``{"ok": True}`` dict on success, or
    ``{"ok": False, "error": "<message>"}`` on failure — including
    ``NotImplementedError`` for commands deferred to v2.
    """
    cmd = payload.get("command")
    try:
        if cmd == "aggregate_episode":
            orchestrator.aggregate_episode(payload["episode_id"])
            return {"ok": True}
        if cmd == "retry_aggregation":
            orchestrator.retry_aggregation(payload["job_id"])
            return {"ok": True}
        if cmd == "cancel_aggregation":
            orchestrator.cancel_aggregation(payload["job_id"])
            return {"ok": True}
        if cmd == "aggregate_all_pending":
            result = orchestrator.aggregate_all_pending()
            return {"ok": True, **result}
        if cmd == "add_go3s_stream":
            try:
                from syncfield.adapters.insta360_go3s import Go3SStream
            except ImportError as e:
                return {"ok": False, "error": f"Go3S adapter not available: {e}"}
            stream_id = payload.get("stream_id") or _next_default_id(orchestrator, "go3s_cam")
            stream = Go3SStream(
                stream_id=stream_id,
                ble_address=payload["address"],
                output_dir=orchestrator.output_dir,
            )
            orchestrator.add(stream)
            return {"ok": True, "stream_id": stream.id}
        return {"ok": False, "error": f"unknown command: {cmd}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


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
        self._agg_state: Optional[AggregationSnapshot] = None
        self._recent_agg_jobs: list = []
        self._agg_lock = threading.Lock()

        self.app = FastAPI(title=title, docs_url=None, redoc_url=None)
        self._setup_middleware()
        self._setup_routes()
        self._setup_cluster_routes()
        self._setup_task_routes()
        self._setup_episode_routes()
        self._setup_static()
        _attach_aggregation_listener(self)

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

        # Multiplexed SSE — fan-out for all sensor streams over one
        # HTTP connection. Used by the frontend to avoid the browser's
        # HTTP/1.1 6-per-origin connection cap, which otherwise queues
        # the 4th+ per-stream EventSource indefinitely.
        @app.get("/stream/sensors")
        async def stream_sensors() -> StreamingResponse:
            return StreamingResponse(
                self._sse_multiplex_generator(),
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
    # Multi-host cluster routes
    # ------------------------------------------------------------------

    def _setup_cluster_routes(self) -> None:
        """Register cluster-aware REST endpoints.

        Every endpoint short-circuits with HTTP 409 when the
        orchestrator has no multi-host role — single-host sessions
        see no cluster features. Leader-only endpoints
        (``start``/``stop``/``collect``) gate on :class:`LeaderRole`.

        Peer fan-out uses ``httpx.AsyncClient`` with a short per-peer
        timeout; per-peer failures become ``status="unreachable"``
        entries in the response without aborting the aggregation.
        """
        app = self.app

        @app.get("/api/cluster/peers")
        async def api_cluster_peers() -> JSONResponse:
            orch = self._session
            _require_multihost(orch)

            announcements = await asyncio.to_thread(
                self._snapshot_peer_announcements
            )
            pending_host_ids = await asyncio.to_thread(
                self._snapshot_pending_host_ids
            )
            peers = self._build_peer_list(announcements, pending_host_ids)
            return JSONResponse({
                "session_id": orch.session_id,
                "self_host_id": orch.host_id,
                "self_role": orch._role.kind,
                "peers": peers,
            })

        @app.get("/api/cluster/health")
        async def api_cluster_health() -> JSONResponse:
            orch = self._session
            _require_multihost(orch)

            announcements = await asyncio.to_thread(
                self._snapshot_peer_announcements
            )
            hosts = await self._fan_out_health(announcements)
            return JSONResponse({
                "session_id": orch.session_id,
                "hosts": hosts,
            })

        @app.post("/api/cluster/devices/discover")
        async def api_cluster_devices_discover(
            request: Request,
        ) -> JSONResponse:
            orch = self._session
            _require_multihost(orch)

            try:
                body = await request.json()
            except Exception:
                body = {}
            kinds = body.get("kinds") if isinstance(body, dict) else None
            timeout = (
                body.get("timeout", 10.0)
                if isinstance(body, dict)
                else 10.0
            )

            announcements = await asyncio.to_thread(
                self._snapshot_peer_announcements
            )
            hosts = await self._fan_out_device_discovery(
                announcements, kinds=kinds, timeout=float(timeout),
            )
            return JSONResponse({"hosts": hosts})

        @app.post("/api/cluster/start")
        async def api_cluster_start() -> JSONResponse:
            orch = self._session
            _require_leader(orch)

            from syncfield.types import SessionState

            announcements = await asyncio.to_thread(
                self._snapshot_peer_announcements
            )

            # The leader's own session.start() is what flips its mDNS
            # advert from "preparing" to "recording" — which is the
            # signal auto-discover followers inside _maybe_wait_for_leader
            # are blocking on. Running it in parallel with the fan-out
            # lets followers that called start() on their own unblock
            # via the advert, while the fan-out still covers followers
            # that only called connect().
            leader_task: Optional[asyncio.Task] = None
            if orch.state is not SessionState.RECORDING:
                leader_task = asyncio.create_task(
                    self._start_recording(countdown_s=3)
                )

            hosts = await self._fan_out_session_command(
                announcements, path="/session/start",
            )

            leader_error: Optional[str] = None
            if leader_task is not None:
                try:
                    await leader_task
                except Exception as exc:
                    logger.exception(
                        "Leader start failed during cluster start"
                    )
                    leader_error = str(exc)

            payload: Dict[str, Any] = {"hosts": hosts}
            if leader_error is not None:
                payload["leader_error"] = leader_error
                return JSONResponse(payload, status_code=500)
            return JSONResponse(payload)

        @app.post("/api/cluster/stop")
        async def api_cluster_stop() -> JSONResponse:
            orch = self._session
            _require_leader(orch)

            from syncfield.types import SessionState

            announcements = await asyncio.to_thread(
                self._snapshot_peer_announcements
            )

            # Mirror api_cluster_start: the leader's own session.stop()
            # flips the advert to "stopped", which followers observing
            # via wait_for_stopped depend on. Fan-out alone doesn't stop
            # the leader.
            leader_task: Optional[asyncio.Task] = None
            if orch.state is SessionState.RECORDING:
                leader_task = asyncio.create_task(self._stop_and_report())

            hosts = await self._fan_out_session_command(
                announcements, path="/session/stop",
            )

            leader_error: Optional[str] = None
            if leader_task is not None:
                try:
                    await leader_task
                except Exception as exc:
                    logger.exception(
                        "Leader stop failed during cluster stop"
                    )
                    leader_error = str(exc)

            payload: Dict[str, Any] = {"hosts": hosts}
            if leader_error is not None:
                payload["leader_error"] = leader_error
                return JSONResponse(payload, status_code=500)
            return JSONResponse(payload)

        @app.post("/api/cluster/collect")
        async def api_cluster_collect() -> JSONResponse:
            orch = self._session
            _require_leader(orch)

            # Guard: collecting while still recording would race the
            # followers' file writers. The follower control planes
            # remain alive for `keep_alive_after_stop_sec` specifically
            # to give the leader time to pull files AFTER stop().
            from syncfield.types import SessionState

            if orch.state is SessionState.RECORDING:
                raise HTTPException(
                    status_code=409,
                    detail="session must be stopped before collecting",
                )

            try:
                manifest = await asyncio.wait_for(
                    asyncio.to_thread(orch.collect_from_followers),
                    timeout=300.0,
                )
            except asyncio.TimeoutError:
                raise HTTPException(
                    status_code=504,
                    detail="collect_from_followers timed out after 300s",
                )
            except Exception as exc:
                logger.exception("collect_from_followers failed")
                raise HTTPException(status_code=500, detail=str(exc))
            return JSONResponse(manifest)

        @app.get("/api/cluster/config")
        async def api_cluster_config() -> JSONResponse:
            orch = self._session
            _require_multihost(orch)

            applied = orch._applied_session_config
            return JSONResponse({
                "session_id": orch.session_id,
                "applied_config": applied.to_dict() if applied is not None else None,
            })

    # ------------------------------------------------------------------
    # Cluster helpers
    # ------------------------------------------------------------------

    def _snapshot_peer_announcements(self) -> List[Any]:
        """Return a snapshot of every peer announcement we can observe.

        Prefers the orchestrator's long-lived browser when one exists
        (followers hold a browser for the whole session). Otherwise
        bootstraps a short-lived :class:`SessionBrowser` scoped to this
        session and sleeps ~1.2s to let mDNS converge, then closes it.

        Runs synchronously — callers should wrap in ``asyncio.to_thread``
        to avoid blocking the event loop.
        """
        orch = self._session
        session_id = orch.session_id
        browser = getattr(orch, "_browser", None)
        if browser is not None:
            try:
                return [
                    ann for ann in browser.current_sessions()
                    if session_id is None or ann.session_id == session_id
                ]
            except Exception:
                logger.exception(
                    "orchestrator browser current_sessions() failed; "
                    "falling back to a short-lived browser",
                )

        # Bootstrap a short-lived browser filtered to this session.
        try:
            from syncfield.multihost.browser import SessionBrowser
        except Exception:
            logger.exception("SessionBrowser import failed")
            return []
        try:
            b = SessionBrowser(session_id=session_id)
            b.start()
        except Exception:
            logger.exception("SessionBrowser failed to start")
            return []
        try:
            # mDNS typically converges within ~1s on a quiet LAN.
            time.sleep(1.2)
            return list(b.current_sessions())
        finally:
            try:
                b.close()
            except Exception:
                logger.debug("SessionBrowser close raised", exc_info=True)

    def _snapshot_pending_host_ids(self) -> List[str]:
        """Return the host_ids of peers whose TXT is still resolving.

        Parses ``<session_id>--<host_id>`` from the mDNS instance names
        the orchestrator's browser has seen via PTR but not yet resolved
        via SRV/TXT. Used by ``/api/cluster/peers`` so the UI shows a
        "resolving…" row the moment the peer appears on the network,
        instead of waiting for the dns-sd fallback to complete.
        """
        orch = self._session
        browser = getattr(orch, "_browser", None)
        if browser is None:
            return []
        try:
            names = browser.pending_peer_names()
        except AttributeError:
            return []  # older browser without pending tracking
        except Exception:
            logger.debug("pending_peer_names failed", exc_info=True)
            return []

        host_ids: List[str] = []
        for name in names:
            instance = name.split(".", 1)[0]
            # Format: "<session_id>--<host_id>"
            if "--" in instance:
                host_ids.append(instance.split("--", 1)[1])
            else:
                host_ids.append(instance)
        return host_ids

    def _build_peer_list(
        self,
        announcements: List[Any],
        pending_host_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Assemble the peer list response (self + other announcements).

        Per the endpoint contract, role derivation is pragmatic:

        - For self, use the local orchestrator's role kind.
        - For a follower viewer, the observed leader gets ``"leader"``
          and all other hosts default to ``"follower"``.
        - For a leader viewer, every non-self announcement defaults to
          ``"follower"`` (leaders do not advertise other leaders).
        """
        orch = self._session
        self_host_id = orch.host_id
        self_role_kind = orch._role.kind if orch._role is not None else "follower"

        leader_host_id: Optional[str] = None
        if self_role_kind == "leader":
            leader_host_id = self_host_id
        else:
            observed = getattr(orch, "_observed_leader", None)
            if observed is not None:
                leader_host_id = observed.host_id

        peers: List[Dict[str, Any]] = []
        seen_host_ids: Set[str] = set()

        # Self entry first — the UI uses the ordering as a hint.
        # Pull our own chirp_enabled / sdk_version / control_plane_port
        # from the advertiser if one is running, else synthesize.
        self_info = self._self_peer_entry(self_role_kind)
        peers.append(self_info)
        seen_host_ids.add(self_host_id)

        for ann in announcements:
            if ann.host_id in seen_host_ids:
                continue
            seen_host_ids.add(ann.host_id)
            if ann.host_id == leader_host_id:
                role = "leader"
            else:
                role = "follower"
            peers.append({
                "host_id": ann.host_id,
                "role": role,
                "status": ann.status,
                "sdk_version": ann.sdk_version,
                "chirp_enabled": ann.chirp_enabled,
                "control_plane_port": ann.control_plane_port,
                "resolved_address": ann.resolved_address,
                "is_self": False,
                "reachable": None,
            })

        # Tentative entries for peers the browser has seen via PTR but
        # hasn't finished resolving (mDNS SRV/TXT can take several
        # seconds cross-network). Surfacing them as "resolving" lets
        # the operator see that the peer IS on the way — previously
        # the UI showed "no peers discovered" until full resolution.
        for host_id in pending_host_ids or []:
            if host_id in seen_host_ids:
                continue
            seen_host_ids.add(host_id)
            peers.append({
                "host_id": host_id,
                "role": "follower"
                if self_role_kind == "leader"
                else "follower",
                "status": "resolving",
                "sdk_version": "",
                "chirp_enabled": False,
                "control_plane_port": None,
                "resolved_address": None,
                "is_self": False,
                "reachable": None,
            })
        return peers

    def _self_peer_entry(self, role: str) -> Dict[str, Any]:
        """Synthesize the peer-list entry for this host."""
        orch = self._session
        try:
            from importlib.metadata import version

            sdk_version = version("syncfield")
        except Exception:
            sdk_version = "unknown"

        chirp_enabled = True
        control_plane_port: Optional[int] = None
        resolved_address: Optional[str] = None

        cp = getattr(orch, "_control_plane", None)
        if cp is not None:
            control_plane_port = getattr(cp, "actual_port", None)

        # Look up our own advertisement in the browser when available
        # to recover chirp_enabled and resolved_address.
        browser = getattr(orch, "_browser", None)
        if browser is not None:
            try:
                for ann in browser.current_sessions():
                    if ann.host_id == orch.host_id:
                        chirp_enabled = ann.chirp_enabled
                        if control_plane_port is None:
                            control_plane_port = ann.control_plane_port
                        resolved_address = ann.resolved_address
                        break
            except Exception:
                logger.debug(
                    "self-lookup via browser failed", exc_info=True
                )

        return {
            "host_id": orch.host_id,
            "role": role,
            "status": orch.state.value,
            "sdk_version": sdk_version,
            "chirp_enabled": chirp_enabled,
            "control_plane_port": control_plane_port,
            "resolved_address": resolved_address,
            "is_self": True,
            "reachable": True,
        }

    def _self_health_entry(self) -> Dict[str, Any]:
        """Return a synthetic health+streams block for this host.

        Mirrors the shape of ``ControlPlaneServer``'s ``/health`` and
        ``/streams`` endpoints by constructing an adapter around the
        orchestrator and reading the same attributes those handlers use.
        """
        orch = self._session
        try:
            from syncfield.orchestrator import _ControlPlaneOrchestratorAdapter

            adapter = _ControlPlaneOrchestratorAdapter(orch)
            health = {
                "host_id": adapter.host_id,
                "role": adapter.role_kind,
                "state": adapter.state_name,
                "sdk_version": adapter.sdk_version,
                "uptime_s": None,
            }
            streams_snapshot = adapter.snapshot_stream_metrics()
        except Exception:
            logger.exception("self health synthesis failed")
            health = {
                "host_id": orch.host_id,
                "role": orch._role.kind if orch._role else None,
                "state": orch.state.value,
                "sdk_version": "unknown",
                "uptime_s": None,
            }
            streams_snapshot = []

        streams = [
            {
                "id": m.id,
                "kind": m.kind,
                "fps": m.fps,
                "frames": m.frames,
                "dropped": m.dropped,
                "last_frame_ns": m.last_frame_ns,
                "bytes_written": m.bytes_written,
            }
            for m in streams_snapshot
        ]
        return {
            "host_id": orch.host_id,
            "is_self": True,
            "status": "ok",
            "rtt_ms": None,
            "health": health,
            "streams": streams,
        }

    async def _fan_out_health(
        self, announcements: List[Any]
    ) -> List[Dict[str, Any]]:
        """Fetch ``/health`` + ``/streams`` from every non-self peer."""
        import httpx

        orch = self._session
        self_host_id = orch.host_id
        results: List[Dict[str, Any]] = [self._self_health_entry()]

        # Track host_ids we've already included to suppress any self
        # announcement the browser might have picked up.
        included: Set[str] = {self_host_id}
        peers = [
            ann for ann in announcements
            if ann.host_id != self_host_id
            and ann.host_id not in included
            and ann.control_plane_port is not None
        ]
        for ann in peers:
            included.add(ann.host_id)

        if not peers:
            return results

        headers = {"Authorization": f"Bearer {orch.session_id}"}

        async def _probe(ann) -> Dict[str, Any]:
            base = orch._follower_base_url(ann)
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    health_resp = await client.get(
                        f"{base}/health", headers=headers,
                    )
                    rtt_ms = round((time.monotonic() - started) * 1000.0, 1)
                    if health_resp.status_code != 200:
                        return {
                            "host_id": ann.host_id,
                            "is_self": False,
                            "status": "unreachable",
                            "error": f"health HTTP {health_resp.status_code}",
                        }
                    streams_resp = await client.get(
                        f"{base}/streams", headers=headers,
                    )
                    if streams_resp.status_code != 200:
                        streams_data: List[Dict[str, Any]] = []
                    else:
                        streams_data = (
                            streams_resp.json().get("streams", [])
                        )
                return {
                    "host_id": ann.host_id,
                    "is_self": False,
                    "status": "ok",
                    "rtt_ms": rtt_ms,
                    "health": health_resp.json(),
                    "streams": streams_data,
                }
            except Exception as exc:
                return {
                    "host_id": ann.host_id,
                    "is_self": False,
                    "status": "unreachable",
                    "error": str(exc),
                }

        peer_results = await asyncio.gather(
            *(_probe(a) for a in peers), return_exceptions=False,
        )
        results.extend(peer_results)
        return results

    async def _fan_out_device_discovery(
        self,
        announcements: List[Any],
        *,
        kinds: Optional[List[str]],
        timeout: float,
    ) -> List[Dict[str, Any]]:
        """Run ``/devices/discover`` on every peer; self runs locally."""
        import httpx

        orch = self._session
        self_host_id = orch.host_id

        # Self: run discovery in-process so we don't need a loopback
        # HTTP hop and so we remain callable even if our own control
        # plane hasn't finished booting.
        async def _self_discover() -> Dict[str, Any]:
            try:
                import syncfield.adapters  # noqa: F401 — register discoverers
                from syncfield.discovery import scan
            except ImportError as exc:
                return {
                    "host_id": self_host_id,
                    "is_self": True,
                    "status": "error",
                    "error": f"discovery unavailable: {exc}",
                    "devices": [],
                }
            try:
                report = await asyncio.to_thread(
                    scan, kinds=kinds, timeout=timeout, use_cache=False,
                )
            except Exception as exc:
                logger.exception("self device discovery failed")
                return {
                    "host_id": self_host_id,
                    "is_self": True,
                    "status": "error",
                    "error": str(exc),
                    "devices": [],
                }
            return {
                "host_id": self_host_id,
                "is_self": True,
                "status": "ok",
                "devices": [
                    {
                        "adapter_type": d.adapter_type,
                        "kind": d.kind,
                        "display_name": d.display_name,
                        "device_id": d.device_id,
                        "in_use": d.in_use,
                        "warnings": list(d.warnings),
                        "accepts_output_dir": d.accepts_output_dir,
                        "description": d.description,
                    }
                    for d in report.devices
                ],
                "errors": dict(report.errors),
                "timed_out": list(report.timed_out),
                "duration_s": report.duration_s,
            }

        headers = {"Authorization": f"Bearer {orch.session_id}"}
        kinds_qs = ",".join(kinds) if kinds else ""
        # httpx's client-level timeout should comfortably exceed the
        # per-discoverer timeout so a cooperating peer has time to
        # respond.
        client_timeout = max(timeout + 5.0, 15.0)

        async def _peer_discover(ann) -> Dict[str, Any]:
            base = orch._follower_base_url(ann)
            params: Dict[str, Any] = {"timeout": timeout}
            if kinds_qs:
                params["kinds"] = kinds_qs
            try:
                async with httpx.AsyncClient(timeout=client_timeout) as client:
                    resp = await client.get(
                        f"{base}/devices/discover",
                        params=params,
                        headers=headers,
                    )
                    if resp.status_code != 200:
                        return {
                            "host_id": ann.host_id,
                            "is_self": False,
                            "status": "error",
                            "error": f"HTTP {resp.status_code}",
                            "devices": [],
                        }
                    data = resp.json()
                return {
                    "host_id": ann.host_id,
                    "is_self": False,
                    "status": "ok",
                    "devices": data.get("devices", []),
                    "errors": data.get("errors", {}),
                    "timed_out": data.get("timed_out", []),
                    "duration_s": data.get("duration_s"),
                }
            except Exception as exc:
                return {
                    "host_id": ann.host_id,
                    "is_self": False,
                    "status": "unreachable",
                    "error": str(exc),
                    "devices": [],
                }

        peers = [
            ann for ann in announcements
            if ann.host_id != self_host_id and ann.control_plane_port is not None
        ]

        tasks = [_self_discover()] + [_peer_discover(a) for a in peers]
        return list(await asyncio.gather(*tasks))

    async def _fan_out_session_command(
        self, announcements: List[Any], *, path: str,
    ) -> List[Dict[str, Any]]:
        """POST *path* (``/session/start`` or ``/session/stop``) to every follower.

        Leader's own start/stop is intentionally NOT triggered here —
        that flow continues to go through the viewer's WebSocket.
        """
        import httpx

        orch = self._session
        self_host_id = orch.host_id
        headers = {"Authorization": f"Bearer {orch.session_id}"}

        peers = [
            ann for ann in announcements
            if ann.host_id != self_host_id and ann.control_plane_port is not None
        ]
        if not peers:
            return []

        async def _send(ann) -> Dict[str, Any]:
            base = orch._follower_base_url(ann)
            try:
                # Followers block inside trigger_start/trigger_stop for
                # the full countdown + chirp duration; give them room.
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{base}{path}", headers=headers,
                    )
                    if resp.status_code != 200:
                        try:
                            detail = resp.json().get(
                                "detail", f"HTTP {resp.status_code}",
                            )
                        except Exception:
                            detail = f"HTTP {resp.status_code}"
                        return {
                            "host_id": ann.host_id,
                            "status": "error",
                            "error": str(detail),
                        }
                    try:
                        data = resp.json()
                        state = data.get("state")
                    except Exception:
                        state = None
                    return {
                        "host_id": ann.host_id,
                        "status": "ok",
                        "state": state,
                    }
            except Exception as exc:
                return {
                    "host_id": ann.host_id,
                    "status": "error",
                    "error": str(exc),
                }

        return list(await asyncio.gather(*(_send(a) for a in peers)))

    # ------------------------------------------------------------------
    # Task management routes
    # ------------------------------------------------------------------

    def _setup_task_routes(self) -> None:
        """CRUD for task list stored in the data root as tasks.json."""
        app = self.app
        data_root = self._session.output_dir.parent

        def _tasks_path() -> Path:
            return data_root / "tasks.json"

        def _read_tasks() -> List[Dict[str, Any]]:
            p = _tasks_path()
            if not p.exists():
                return []
            return json.loads(p.read_text())

        def _write_tasks(tasks: List[Dict[str, Any]]) -> None:
            _tasks_path().write_text(json.dumps(tasks, indent=2))

        @app.get("/api/tasks")
        async def api_list_tasks() -> JSONResponse:
            tasks = await asyncio.to_thread(_read_tasks)
            return JSONResponse({"tasks": tasks})

        @app.post("/api/tasks")
        async def api_create_task(request: Request) -> JSONResponse:
            body = await request.json()
            name = body.get("name", "").strip()
            if not name:
                return JSONResponse({"error": "Task name is required"}, status_code=400)

            def _create():
                tasks = _read_tasks()
                if any(t["name"] == name for t in tasks):
                    return None  # duplicate
                task = {"name": name}
                tasks.append(task)
                _write_tasks(tasks)
                return task

            task = await asyncio.to_thread(_create)
            if task is None:
                return JSONResponse({"error": f"Task '{name}' already exists"}, status_code=409)
            return JSONResponse(task, status_code=201)

        @app.put("/api/tasks/{task_name}")
        async def api_update_task(task_name: str, request: Request) -> JSONResponse:
            body = await request.json()

            def _update():
                tasks = _read_tasks()
                for t in tasks:
                    if t["name"] == task_name:
                        if "name" in body:
                            t["name"] = body["name"]
                        _write_tasks(tasks)
                        return t
                return None

            task = await asyncio.to_thread(_update)
            if task is None:
                return JSONResponse({"error": "Task not found"}, status_code=404)
            return JSONResponse(task)

        @app.delete("/api/tasks/{task_name}")
        async def api_delete_task(task_name: str) -> JSONResponse:
            def _delete():
                tasks = _read_tasks()
                new_tasks = [t for t in tasks if t["name"] != task_name]
                if len(new_tasks) == len(tasks):
                    return False
                _write_tasks(new_tasks)
                return True

            deleted = await asyncio.to_thread(_delete)
            if not deleted:
                return JSONResponse({"error": "Task not found"}, status_code=404)
            return JSONResponse({"status": "deleted"})

        @app.post("/api/task/select")
        async def api_select_task(request: Request) -> JSONResponse:
            """Set the current task for the next recording."""
            body = await request.json()
            task = body.get("task", None)
            self._session.task = task
            return JSONResponse({"task": task})

        @app.get("/api/task/current")
        async def api_current_task() -> JSONResponse:
            return JSONResponse({"task": self._session.task})

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
                    {"error": "No recorded data in this episode (manifest.json not found). Record a session first."},
                    status_code=400,
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

        @app.get("/api/episodes/{episode_id}/waveform/{stream_id}")
        async def api_waveform(
            episode_id: str, stream_id: str,
        ) -> JSONResponse:
            """Return a downsampled waveform envelope from a WAV file."""
            ep_dir = self._session.output_dir.parent / episode_id
            wav_path = ep_dir / f"{stream_id}.wav"
            if not wav_path.is_file():
                return JSONResponse(
                    {"error": f"WAV file not found: {stream_id}.wav"},
                    status_code=404,
                )

            try:
                envelope = await asyncio.to_thread(
                    _read_wav_envelope, wav_path, 1000,
                )
                return JSONResponse(envelope)
            except Exception as exc:
                logger.exception("Failed to read waveform")
                return JSONResponse({"error": str(exc)}, status_code=500)

        @app.get("/api/episodes/{episode_id}/sensor/{stream_id}")
        async def api_episode_sensor(
            episode_id: str, stream_id: str,
        ) -> JSONResponse:
            """Return recorded sensor samples for Review-mode playback.

            Reads ``{stream_id}.jsonl`` (one ``SensorSample`` per line)
            and returns a compact per-channel representation:

              ``{t: float[], channels: {name: float[]}, count, duration_s}``

            ``t`` is seconds relative to the first sample so the
            frontend can align with ``<video>.currentTime``. When the
            recording has more than ``MAX_POINTS`` samples we stride-
            decimate so the payload stays small and SVG rendering stays
            cheap — the resulting curve is faithful because inter-
            sample period is already ≤5 ms for 200 Hz IMUs.
            """
            ep_dir = self._session.output_dir.parent / episode_id
            jsonl_path = ep_dir / f"{stream_id}.jsonl"
            if not jsonl_path.is_file():
                return JSONResponse(
                    {"error": f"Sensor file not found: {stream_id}.jsonl"},
                    status_code=404,
                )

            try:
                data = await asyncio.to_thread(_read_sensor_jsonl, jsonl_path)
                return JSONResponse(data)
            except Exception as exc:
                logger.exception("Failed to read sensor jsonl")
                return JSONResponse({"error": str(exc)}, status_code=500)

    # ------------------------------------------------------------------
    # Static files (built React app)
    # ------------------------------------------------------------------

    def _setup_static(self) -> None:
        if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
            # Serve the SPA — catch-all returns index.html for client-side routing.
            #
            # Cache strategy (standard Vite hashed-asset pattern):
            #   * index.html → no-store, must revalidate every request.
            #     Without this, browsers apply heuristic caching to the
            #     SPA shell — a stale index.html keeps pointing at last
            #     deploy's hashed bundle even after we ship a new one,
            #     so users see fixed bugs come back.
            #   * /assets/index-<hash>.{js,css} → 1 year immutable.
            #     The filename changes per build, so cache forever is safe.
            #   * Everything else → no-store (small static files).
            @self.app.get("/{full_path:path}")
            async def spa_fallback(full_path: str) -> HTMLResponse:
                file_path = STATIC_DIR / full_path
                if full_path and file_path.exists() and file_path.is_file():
                    content = file_path.read_bytes()
                    media_type = _guess_media_type(full_path)
                    if full_path.startswith("assets/") and (
                        full_path.endswith(".js") or full_path.endswith(".css")
                    ):
                        cache_header = "public, max-age=31536000, immutable"
                    else:
                        cache_header = "no-store"
                    return HTMLResponse(
                        content=content,
                        media_type=media_type,
                        headers={"Cache-Control": cache_header},
                    )
                # Fallback to index.html for SPA routing — never cache.
                return HTMLResponse(
                    content=(STATIC_DIR / "index.html").read_text(),
                    headers={"Cache-Control": "no-store"},
                )

    # ------------------------------------------------------------------
    # WebSocket broadcast loop
    # ------------------------------------------------------------------

    async def _broadcast_loop(self, ws: WebSocket) -> None:
        """Send snapshot JSON to a single WebSocket client at ~10 Hz."""
        while True:
            snapshot = self._poller.get_snapshot()
            if snapshot is not None:
                try:
                    # Inject current aggregation state if the poller snapshot
                    # doesn't carry one (the default case for non-Go3S sessions).
                    if snapshot.aggregation is None and self._agg_state is not None:
                        snapshot = dataclasses.replace(
                            snapshot, aggregation=self._agg_state
                        )
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
            await self._stop_and_report()
        elif action == "cancel":
            try:
                await self._broadcast_message({
                    "type": "stop_result",
                    "status": "saving",
                })
                await asyncio.to_thread(self._session.cancel)
                await self._broadcast_message({
                    "type": "stop_result",
                    "status": "success",
                    "cancelled": True,
                    "streams": {},
                })
            except Exception as exc:
                logger.exception("Cancel failed")
                await self._broadcast_message({
                    "type": "stop_result",
                    "status": "error",
                    "error": f"Cancel failed: {exc}",
                    "streams": {},
                })
        elif action in (
            "retry_aggregation",
            "cancel_aggregation",
            "aggregate_episode",
            "aggregate_all_pending",
        ):
            # Route aggregation control commands through the T14 dispatcher.
            # The payload uses the same key names expected by handle_control_command
            # (it reads "command" from the dict), so we normalise here.
            agg_payload = {**msg, "command": action}
            result = await asyncio.to_thread(
                handle_control_command, self._session, agg_payload
            )
            if not result.get("ok"):
                logger.warning("Aggregation command %r failed: %s", action, result.get("error"))
            # Echo a typed result back so the UI can surface a toast/banner.
            await self._broadcast_message({
                "type": f"{action}_result",
                **result,
            })
        elif action == "add_go3s_stream":
            # Route Go3S stream-add command through the T17 dispatcher.
            go3s_payload = {**msg, "command": action}
            result = await asyncio.to_thread(
                handle_control_command, self._session, go3s_payload
            )
            if not result.get("ok"):
                logger.warning("add_go3s_stream failed: %s", result.get("error"))
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

        try:
            await asyncio.to_thread(
                self._session.start,
                countdown_s=countdown_s,
                on_countdown_tick=_on_tick,
            )
        except Exception as exc:
            # Surface start failures to the browser so the Record button
            # doesn't silently appear to do nothing. The WebSocket
            # top-level handler swallows exceptions with a debug log.
            logger.exception("session.start() failed")
            await self._broadcast_message({
                "type": "record_error",
                "error": f"{type(exc).__name__}: {exc}",
            })
            raise

    async def _stop_and_report(self) -> None:
        """Stop recording, validate output, and broadcast the result.

        Sends a ``stop_result`` WebSocket message with per-stream
        status so the viewer can show success/failure immediately.
        """
        # Broadcast "saving" state
        await self._broadcast_message({
            "type": "stop_result",
            "status": "saving",
        })

        try:
            report = await asyncio.to_thread(self._session.stop)
        except Exception as exc:
            await self._broadcast_message({
                "type": "stop_result",
                "status": "error",
                "error": str(exc),
                "streams": {},
            })
            return

        # After stop(), session.output_dir has rotated to the *next*
        # episode path. The files we just wrote live under
        # last_episode_dir — fall back to output_dir for SDK callers on
        # an older orchestrator that didn't expose the property.
        output_dir = (
            getattr(self._session, "last_episode_dir", None)
            or self._session.output_dir
        )
        stream_results: Dict[str, Any] = {}
        all_ok = True

        for fin in report.finalizations:
            result: Dict[str, Any] = {
                "status": fin.status,
                "frame_count": fin.frame_count,
            }

            # Check expected output files exist on disk
            if fin.file_path is not None:
                file_exists = Path(fin.file_path).exists()
                result["file_path"] = str(fin.file_path)
                result["file_exists"] = file_exists
                if not file_exists:
                    result["status"] = "failed"
                    result["error"] = f"Output file missing: {fin.file_path}"
                    all_ok = False
            else:
                # Sensor streams: check for timestamps JSONL
                ts_path = output_dir / f"{fin.stream_id}.timestamps.jsonl"
                jsonl_path = output_dir / f"{fin.stream_id}.jsonl"
                has_output = ts_path.exists() or jsonl_path.exists()
                result["has_output"] = has_output

            if fin.status == "failed":
                all_ok = False
                result["error"] = fin.error

            if fin.frame_count == 0 and fin.status == "completed":
                result["warning"] = "No frames captured"

            stream_results[fin.stream_id] = result

        # Check manifest and sync_point were written
        manifest_ok = (output_dir / "manifest.json").exists()
        sync_point_ok = (output_dir / "sync_point.json").exists()

        await self._broadcast_message({
            "type": "stop_result",
            "status": "success" if all_ok else "partial",
            "output_dir": str(output_dir),
            "manifest_ok": manifest_ok,
            "sync_point_ok": sync_point_ok,
            "streams": stream_results,
        })

    async def _broadcast_message(self, message: dict) -> None:
        """Send a JSON message to all connected WebSocket clients."""
        text = json.dumps(message)
        disconnected: List[WebSocket] = []
        for ws in self._ws_clients:
            try:
                await ws.send_text(text)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            self._ws_clients.discard(ws)

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
                        # BGR numpy → PIL Image (PIL expects RGB, so reverse the last axis).
                        rgb = stream.latest_frame[:, :, ::-1]
                        img = Image.fromarray(rgb)
                        buf = io.BytesIO()
                        img.save(buf, format="JPEG", quality=80)
                        frame_bytes = buf.getvalue()
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
        """Yield sensor data as Server-Sent Events.

        Two payload shapes ride the same event:

        * ``channels`` — scalar latest values (rolling chart).
        * ``pose``    — list-valued latest samples (e.g. 156-float
                        ``hand_joints`` from MetaQuestHandStream).
                        Kept optional so existing sensor-chart callers
                        that only consume scalars stay backward-compatible.

        Emits an SSE comment (``:<text>\\n\\n``) immediately so the
        browser's EventSource fires onopen as soon as headers arrive.
        Without this the response body stays empty until the first real
        data point and the panel sits on "Connecting…" indefinitely —
        especially for BLE sensors in the ~100 ms window before the
        first sample propagates into the poller buffer. Subsequent
        comments every ~5 s keep the connection alive through proxies
        that close idle streams.
        """
        # Header flush — tells the browser the stream is live and unblocks
        # EventSource.onopen. Comment lines are ignored by the SSE parser.
        yield ": connected\n\n"

        ticks_since_data = 0
        while True:
            sent = False
            snapshot = self._poller.get_snapshot()
            if snapshot is not None:
                stream = snapshot.streams.get(stream_id)
                if stream is not None and (stream.plot_points or stream.latest_pose):
                    channels: Dict[str, float] = {}
                    label: Optional[float] = None
                    for ch_name, (xs, ys) in stream.plot_points.items():
                        if ys:
                            channels[ch_name] = ys[-1]
                        if xs and label is None:
                            label = xs[-1]

                    pose = stream.latest_pose if stream.latest_pose else None

                    if channels or pose:
                        event_data = json.dumps({
                            "channels": channels,
                            "pose": pose,
                            "label": label,
                        })
                        yield f"data: {event_data}\n\n"
                        sent = True

            if sent:
                ticks_since_data = 0
            else:
                ticks_since_data += 1
                # Keep-alive comment every ~5 s of dead air so onerror/
                # reconnect logic in the browser never kicks in spuriously.
                if ticks_since_data >= 50:
                    yield ": ka\n\n"
                    ticks_since_data = 0

            await asyncio.sleep(0.1)  # ~10 Hz

    async def _sse_multiplex_generator(self):
        """Yield sensor events for every active stream on one connection.

        Mirrors :meth:`_sse_generator` but fans out across all streams
        in the current snapshot, tagging each event with ``stream_id``
        so the client can dispatch to the correct tile. One shared
        connection sidesteps the browser HTTP/1.1 6-per-origin cap,
        which was leaving per-stream EventSources queued once a session
        exceeded ~4 sensor streams.
        """
        yield ": connected\n\n"

        ticks_since_data = 0
        while True:
            sent = False
            snapshot = self._poller.get_snapshot()
            if snapshot is not None:
                for stream_id, stream in snapshot.streams.items():
                    if not (stream.plot_points or stream.latest_pose):
                        continue
                    channels: Dict[str, float] = {}
                    label: Optional[float] = None
                    for ch_name, (xs, ys) in stream.plot_points.items():
                        if ys:
                            channels[ch_name] = ys[-1]
                        if xs and label is None:
                            label = xs[-1]

                    pose = stream.latest_pose if stream.latest_pose else None

                    if channels or pose:
                        event_data = json.dumps({
                            "stream_id": stream_id,
                            "channels": channels,
                            "pose": pose,
                            "label": label,
                        })
                        yield f"data: {event_data}\n\n"
                        sent = True

            if sent:
                ticks_since_data = 0
            else:
                ticks_since_data += 1
                if ticks_since_data >= 50:
                    yield ": ka\n\n"
                    ticks_since_data = 0

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


def _read_wav_envelope(wav_path: Path, num_points: int = 1000) -> dict:
    """Read a WAV file and return a downsampled min/max envelope.

    Returns a dict with ``sample_rate``, ``duration_s``, ``channels``,
    and ``envelope`` — a list of ``[min, max]`` pairs representing the
    amplitude range within each bucket.
    """
    import struct as struct_mod
    import wave

    with wave.open(str(wav_path), "rb") as wf:
        n_channels = wf.getnchannels()
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        sample_width = wf.getsampwidth()
        raw = wf.readframes(n_frames)

    duration_s = n_frames / sample_rate if sample_rate > 0 else 0

    # Parse PCM samples (16-bit assumed)
    if sample_width == 2:
        fmt = f"<{n_frames * n_channels}h"
        samples = list(struct_mod.unpack(fmt, raw))
    else:
        # Fallback: treat as unsigned bytes
        samples = [b - 128 for b in raw]

    # Take first channel only for mono envelope
    if n_channels > 1:
        samples = samples[::n_channels]

    # Downsample to num_points buckets
    total = len(samples)
    if total == 0:
        return {
            "sample_rate": sample_rate,
            "duration_s": duration_s,
            "channels": n_channels,
            "envelope": [],
        }

    bucket_size = max(1, total // num_points)
    envelope = []
    for i in range(0, total, bucket_size):
        bucket = samples[i : i + bucket_size]
        lo = min(bucket) / 32768.0
        hi = max(bucket) / 32768.0
        envelope.append([round(lo, 4), round(hi, 4)])

    return {
        "sample_rate": sample_rate,
        "duration_s": round(duration_s, 3),
        "channels": n_channels,
        "envelope": envelope,
    }


def _read_sensor_jsonl(jsonl_path: Path, max_points: int = 2000) -> dict:
    """Read a sensor ``{stream_id}.jsonl`` and return a compact playback form.

    Each line is a :class:`~syncfield.types.SensorSample` dict. We
    convert to column-oriented arrays (one per channel) plus a single
    ``t`` array of seconds-relative-to-first-sample — the shape the
    viewer's Review mode consumes. Files with more than ``max_points``
    rows are stride-decimated to keep the payload small; at 200 Hz
    this still yields >5 ms resolution per visible point which is
    well below what a human notices in the playback scrubber.

    Non-numeric scalar channels and underscore-prefixed auxiliaries are
    dropped from ``channels``; list-valued channels (e.g. Meta Quest's
    156-float ``hand_joints``) are surfaced separately under
    ``vector_channels`` so the Review-mode pose panel can index them at
    playback time without forcing the line-chart code path to handle
    list samples. The first sample's ``capture_ns`` is used as the zero
    reference for ``t``.
    """
    import json as json_mod

    capture_ns: list[int] = []
    channel_buffers: dict[str, list[float]] = {}
    vector_buffers: dict[str, list[list[float]]] = {}

    with open(jsonl_path, "r") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json_mod.loads(line)
            except json_mod.JSONDecodeError:
                continue
            ts = row.get("capture_ns")
            if ts is None:
                continue
            ch = row.get("channels") or {}

            capture_ns.append(int(ts))
            for name, value in ch.items():
                if name.startswith("_") or "timestamp" in name:
                    continue
                if isinstance(value, (int, float)):
                    buf = channel_buffers.get(name)
                    if buf is None:
                        buf = [float("nan")] * (len(capture_ns) - 1)
                        channel_buffers[name] = buf
                    buf.append(float(value))
                elif isinstance(value, list) and value and all(
                    isinstance(v, (int, float)) for v in value
                ):
                    vbuf = vector_buffers.get(name)
                    if vbuf is None:
                        # Back-fill with zero vectors of the right
                        # dimension so the per-frame index is aligned
                        # with capture_ns.
                        zero = [0.0] * len(value)
                        vbuf = [list(zero) for _ in range(len(capture_ns) - 1)]
                        vector_buffers[name] = vbuf
                    vbuf.append([float(v) for v in value])
            # Pad any channel that didn't appear on this row so all
            # buffers stay the same length as capture_ns.
            for name, buf in channel_buffers.items():
                if len(buf) < len(capture_ns):
                    buf.append(float("nan"))
            for name, vbuf in vector_buffers.items():
                if len(vbuf) < len(capture_ns):
                    dim = len(vbuf[-1]) if vbuf else 0
                    vbuf.append([0.0] * dim)

    total = len(capture_ns)
    if total == 0:
        return {
            "t": [],
            "channels": {},
            "vector_channels": {},
            "count": 0,
            "duration_s": 0.0,
        }

    t0 = capture_ns[0]
    # Stride-decimate if oversized — keep roughly evenly-spaced points
    # plus the last sample so the trailing edge stays accurate.
    stride = max(1, total // max_points)
    keep = list(range(0, total, stride))
    if keep[-1] != total - 1:
        keep.append(total - 1)

    t_seconds = [round((capture_ns[i] - t0) / 1e9, 6) for i in keep]
    channels_out = {
        name: [round(buf[i], 6) for i in keep]
        for name, buf in channel_buffers.items()
    }
    vector_channels_out = {
        name: [vbuf[i] for i in keep]
        for name, vbuf in vector_buffers.items()
    }
    duration_s = round((capture_ns[-1] - t0) / 1e9, 6)

    return {
        "t": t_seconds,
        "channels": channels_out,
        "vector_channels": vector_channels_out,
        "count": total,
        "duration_s": duration_s,
    }


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
