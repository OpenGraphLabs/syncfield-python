"""In-process aiohttp server that mimics the Quest 3 companion Unity app's
HTTP surface, for integration tests that don't want a real headset."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import AsyncIterator

from aiohttp import web


@dataclass
class FakeQuestServer:
    left_mp4: bytes = b""
    right_mp4: bytes = b""
    left_timestamps: bytes = b'{"frame_number":0,"capture_ns":1}\n'
    right_timestamps: bytes = b'{"frame_number":0,"capture_ns":1}\n'

    _state: dict = field(default_factory=lambda: {"recording": False, "session_id": None})

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[str]:
        app = web.Application()
        app.router.add_get("/status", self._status)
        app.router.add_post("/recording/start", self._start)
        app.router.add_post("/recording/stop", self._stop)
        app.router.add_get("/recording/files/left",  self._make_file_handler(lambda: self.left_mp4))
        app.router.add_get("/recording/files/right", self._make_file_handler(lambda: self.right_mp4))
        app.router.add_get("/recording/timestamps/left",  self._make_file_handler(lambda: self.left_timestamps))
        app.router.add_get("/recording/timestamps/right", self._make_file_handler(lambda: self.right_timestamps))

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="127.0.0.1", port=0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            await runner.cleanup()

    async def _status(self, request: web.Request) -> web.Response:
        return web.json_response({
            "recording": self._state["recording"],
            "session_id": self._state["session_id"],
            "last_preview_capture_ns": 0,
            "left_camera_ready": True,
            "right_camera_ready": True,
            "storage_free_bytes": 10_000_000_000,
        })

    async def _start(self, request: web.Request) -> web.Response:
        payload = await request.json()
        self._state["recording"] = True
        self._state["session_id"] = payload["session_id"]
        return web.json_response({
            "session_id": payload["session_id"],
            "quest_mono_ns_at_start": 0,
            "delta_ns": 0,
            "started": True,
        })

    async def _stop(self, request: web.Request) -> web.Response:
        self._state["recording"] = False
        return web.json_response({
            "session_id": self._state["session_id"],
            "left":  {"frame_count": 1, "bytes": len(self.left_mp4),  "last_capture_ns": 1},
            "right": {"frame_count": 1, "bytes": len(self.right_mp4), "last_capture_ns": 1},
            "duration_s": 0.1,
        })

    def _make_file_handler(self, getter):
        async def handler(request: web.Request) -> web.Response:
            data = getter()
            return web.Response(body=data, headers={"Content-Length": str(len(data))})
        return handler
