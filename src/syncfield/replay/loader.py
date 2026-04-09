"""Session-folder loader for the replay viewer.

Reads a directory written by ``syncfield.writer`` and produces a
:class:`ReplayManifest` that the HTTP server can serve as JSON.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

StreamKind = Literal["video", "sensor", "custom"]


@dataclass(frozen=True)
class ReplayStream:
    """One stream's metadata + on-disk locations.

    ``media_path`` is kept on the Python side for the file-serving
    handler; it is intentionally excluded from the JSON view of the
    manifest (see :meth:`ReplayManifest.to_dict`).
    """

    id: str
    kind: StreamKind
    media_url: Optional[str]
    media_path: Optional[Path]
    data_url: Optional[str]
    frame_count: int


@dataclass(frozen=True)
class ReplayManifest:
    """Everything the SPA needs to render a session, in one struct."""

    session_dir: Path
    host_id: str
    sync_point: dict
    streams: list[ReplayStream]
    sync_report: Optional[dict]
    has_frame_map: bool

    def to_dict(self) -> dict[str, Any]:
        """Serializable view — strips Path fields and the sync_report
        (which is served separately via the /api/sync-report route).
        """
        return {
            "host_id": self.host_id,
            "sync_point": self.sync_point,
            "has_frame_map": self.has_frame_map,
            "streams": [
                {
                    "id": s.id,
                    "kind": s.kind,
                    "media_url": s.media_url,
                    "data_url": s.data_url,
                    "frame_count": s.frame_count,
                }
                for s in self.streams
            ],
        }


def load_session(session_dir: Path) -> ReplayManifest:
    """Read a session folder and return its :class:`ReplayManifest`.

    Raises:
        FileNotFoundError: if ``session_dir/manifest.json`` does not exist.
    """
    session_dir = Path(session_dir)
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"manifest.json not found in {session_dir}"
        )

    raw = json.loads(manifest_path.read_text())
    host_id = raw.get("host_id", "")
    streams_raw: dict = raw.get("streams", {})

    streams: list[ReplayStream] = []
    for stream_id, info in streams_raw.items():
        kind: StreamKind = info.get("kind", "custom")
        frame_count = int(info.get("frame_count", 0) or 0)

        media_path: Optional[Path] = None
        media_url: Optional[str] = None
        data_url: Optional[str] = None

        if kind == "video":
            mp4 = session_dir / f"{stream_id}.mp4"
            if mp4.is_file():
                media_path = mp4
                media_url = f"/media/{stream_id}"

        sensor_jsonl = session_dir / f"{stream_id}.jsonl"
        if sensor_jsonl.is_file():
            data_url = f"/data/{stream_id}.jsonl"

        streams.append(
            ReplayStream(
                id=stream_id,
                kind=kind,
                media_url=media_url,
                media_path=media_path,
                data_url=data_url,
                frame_count=frame_count,
            )
        )

    sync_point: dict = {}
    sp_path = session_dir / "sync_point.json"
    if sp_path.is_file():
        sync_point = json.loads(sp_path.read_text())
    else:
        logger.warning("sync_point.json missing in %s", session_dir)

    sync_report: Optional[dict] = None
    sr_path = session_dir / "synced" / "sync_report.json"
    if sr_path.is_file():
        sync_report = json.loads(sr_path.read_text())

    has_frame_map = (session_dir / "synced" / "frame_map.jsonl").is_file()

    return ReplayManifest(
        session_dir=session_dir,
        host_id=host_id,
        sync_point=sync_point,
        streams=streams,
        sync_report=sync_report,
        has_frame_map=has_frame_map,
    )
