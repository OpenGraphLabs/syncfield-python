"""Local HTTP server for the replay viewer.

Serves a small JSON API plus the bundled SPA. Bound to ``127.0.0.1`` by
default — never bind a public interface, the routes assume a trusted
single origin.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
from importlib.resources import as_file, files
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from syncfield.replay._handler import UnsafePathError, safe_resolve
from syncfield.replay.loader import ReplayManifest

logger = logging.getLogger(__name__)


# Holds the importlib.resources `as_file` context for the bundled static
# directory. For force-include wheels (the common case) `as_file` is a
# no-op; for zip-based installs it extracts to a tempdir which we MUST
# keep alive for the lifetime of the server, otherwise the path becomes
# a dangling reference once the context manager exits. We register an
# atexit hook to release it on interpreter shutdown.
_STATIC_RESOURCE_STACK = contextlib.ExitStack()
atexit.register(_STATIC_RESOURCE_STACK.close)


def _static_dir() -> Path:
    """Return the bundled static directory shipped inside the package.

    The returned path is valid for the lifetime of the interpreter — the
    underlying ``as_file`` context is held by a module-level ExitStack
    so that zip-extracted resources are not deleted out from under us.
    """
    pkg_root = files("syncfield.replay").joinpath("static")
    return Path(_STATIC_RESOURCE_STACK.enter_context(as_file(pkg_root)))


def build_app(manifest: ReplayManifest) -> Starlette:
    """Construct a Starlette app bound to a single session manifest."""
    static_dir = _static_dir()
    streams_by_id = {s.id: s for s in manifest.streams}

    async def get_session(_request: Request) -> JSONResponse:
        return JSONResponse(manifest.to_dict())

    async def get_sync_report(_request: Request) -> Response:
        if manifest.sync_report is None:
            return JSONResponse({"detail": "no sync report"}, status_code=404)
        return JSONResponse(manifest.sync_report)

    async def get_media(request: Request) -> Response:
        stream_id = request.path_params["stream_id"]
        stream = streams_by_id.get(stream_id)
        if stream is None or stream.media_path is None:
            raise HTTPException(status_code=404)
        # TODO(v2): infer media_type from stream.media_path.suffix once we
        # ship more than just MP4 (.mov, .mkv, .webm).
        return FileResponse(stream.media_path, media_type="video/mp4")

    async def get_data(request: Request) -> Response:
        filename = request.path_params["filename"]
        try:
            resolved = safe_resolve(manifest.session_dir, filename)
        except UnsafePathError:
            raise HTTPException(status_code=400)
        if resolved is None or not resolved.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(resolved)

    routes = [
        Route("/api/session", get_session),
        Route("/api/sync-report", get_sync_report),
        Route("/media/{stream_id}", get_media),
        Route("/data/{filename:path}", get_data),
        Mount(
            "/",
            app=StaticFiles(directory=str(static_dir), html=True),
            name="static",
        ),
    ]
    return Starlette(routes=routes)


class ReplayServer:
    """Wraps a uvicorn server bound to a single session.

    The instance owns its own ``uvicorn.Server`` so the caller can shut
    it down without touching the global event loop.
    """

    def __init__(
        self,
        manifest: ReplayManifest,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._manifest = manifest
        self._app = build_app(manifest)
        config = uvicorn.Config(
            self._app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)

    @property
    def url(self) -> str:
        """Return ``http://host:port/`` using the actual bound port.

        For ephemeral ports (``port=0``), uvicorn resolves the real port
        during startup and binds it inside the asyncio server's socket
        — *not* back onto ``self._server.config.port``, which stays 0.
        We read from the live socket once it's available, falling back
        to the configured value before startup.
        """
        host = self._server.config.host
        port = self._server.config.port
        if self._server.servers:
            sockets = self._server.servers[0].sockets
            if sockets:
                try:
                    port = sockets[0].getsockname()[1]
                except Exception:
                    pass
        return f"http://{host}:{port}/"

    def serve(self) -> None:
        """Run the server on the calling thread until shutdown."""
        self._server.run()

    def request_shutdown(self) -> None:
        """Ask the underlying uvicorn server to stop after the current request."""
        self._server.should_exit = True
