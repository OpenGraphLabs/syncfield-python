"""Per-stream JSONL writers for timestamp output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import IO, Any

from importlib.metadata import version as _pkg_version

from syncfield.types import FrameTimestamp, SensorSample, SyncPoint


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


class SensorWriter:
    """Writes ``SensorSample`` entries to a per-stream JSONL file.

    Each call to :meth:`write` appends one JSON line and flushes immediately
    so that sensor data is persisted even if the process crashes mid-recording.

    Output file: ``{stream_id}.jsonl``
    """

    def __init__(self, stream_id: str, output_dir: Path) -> None:
        self._stream_id = stream_id
        self._path = output_dir / f"{stream_id}.jsonl"
        self._handle: IO[str] | None = None
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    @property
    def path(self) -> Path:
        return self._path

    def open(self) -> None:
        self._handle = open(self._path, "w")

    def write(self, sample: SensorSample) -> None:
        if self._handle is None:
            raise RuntimeError(f"SensorWriter for '{self._stream_id}' is not open")
        self._handle.write(json.dumps(sample.to_dict(), separators=(",", ":")) + "\n")
        self._handle.flush()
        self._count += 1

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def write_sync_point(sync_point: SyncPoint, output_dir: Path) -> Path:
    """Write ``sync_point.json`` to *output_dir* and return the path."""
    path = output_dir / "sync_point.json"
    data: dict[str, Any] = {"sdk_version": _pkg_version("syncfield")}
    data.update(sync_point.to_dict())
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return path


def write_manifest(
    host_id: str,
    streams: dict[str, dict[str, Any]],
    output_dir: Path,
) -> Path:
    """Write ``manifest.json`` to *output_dir* and return the path."""
    path = output_dir / "manifest.json"
    manifest: dict[str, Any] = {
        "sdk_version": _pkg_version("syncfield"),
        "host_id": host_id,
        "streams": streams,
    }
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    return path
