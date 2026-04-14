"""Typed HTTP client for the Meta Quest 3 companion Unity app.

Wraps :mod:`httpx` with domain-specific request shaping and response
parsing. Accepts a ``transport`` kwarg so unit tests can inject
``httpx.MockTransport`` without spinning up a real server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx


DEFAULT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class QuestStatus:
    """Snapshot of the Quest app's current state (from ``GET /status``)."""

    recording: bool
    session_id: Optional[str]
    last_preview_capture_ns: int
    left_camera_ready: bool
    right_camera_ready: bool
    storage_free_bytes: int

    @classmethod
    def from_json(cls, payload: dict) -> "QuestStatus":
        return cls(
            recording=bool(payload["recording"]),
            session_id=payload.get("session_id"),
            last_preview_capture_ns=int(payload.get("last_preview_capture_ns", 0)),
            left_camera_ready=bool(payload.get("left_camera_ready", False)),
            right_camera_ready=bool(payload.get("right_camera_ready", False)),
            storage_free_bytes=int(payload.get("storage_free_bytes", 0)),
        )


class QuestHttpClient:
    """Thin typed façade over the Quest's HTTP surface (port 14045)."""

    def __init__(
        self,
        host: str,
        port: int = 14045,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base_url = f"http://{host}:{port}"
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "QuestHttpClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def status(self) -> QuestStatus:
        """Fetch a fresh ``QuestStatus`` snapshot from the Quest."""
        response = self._client.get("/status")
        response.raise_for_status()
        return QuestStatus.from_json(response.json())
