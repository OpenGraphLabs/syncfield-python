"""Per-stream JSONL writers for timestamp output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import IO, Any

from syncfield.types import FrameTimestamp, SyncPoint

_SDK_VERSION = "0.1.0"


class StreamWriter:
    """Writes ``FrameTimestamp`` entries to a per-stream JSONL file.

    Each call to :meth:`write` appends one JSON line and flushes immediately
    so that timestamps are persisted even if the process crashes mid-recording.
    """

    def __init__(self, stream_id: str, output_dir: Path) -> None:
        self._stream_id = stream_id
        self._path = output_dir / f"{stream_id}.timestamps.jsonl"
        self._handle: IO[str] | None = None
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    def open(self) -> None:
        self._handle = open(self._path, "w")

    def write(self, ts: FrameTimestamp) -> None:
        if self._handle is None:
            raise RuntimeError(f"StreamWriter for '{self._stream_id}' is not open")
        self._handle.write(json.dumps(ts.to_dict(), separators=(",", ":")) + "\n")
        self._handle.flush()
        self._count += 1

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def write_sync_point(sync_point: SyncPoint, output_dir: Path) -> Path:
    """Write ``sync_point.json`` to *output_dir* and return the path."""
    path = output_dir / "sync_point.json"
    data: dict[str, Any] = {"sdk_version": _SDK_VERSION}
    data.update(sync_point.to_dict())
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return path
