"""Per-stream JSONL writers and session-level artifact writers.

Three classes of writer live here:

- :class:`StreamWriter` — per-stream ``{stream_id}.timestamps.jsonl`` for
  video-style streams that only emit timestamps.
- :class:`SensorWriter` — per-stream ``{stream_id}.jsonl`` for sensor streams
  that embed channel values with each sample.
- :class:`SessionLogWriter` — one-file orchestrator log capturing state
  transitions, health events, and rollbacks. Flushes on every write so the
  log survives a process crash mid-recording.

Two helpers produce the session-level JSON artifacts:

- :func:`write_sync_point` — ``sync_point.json`` (with optional chirp fields).
- :func:`write_manifest` — ``manifest.json`` (arbitrary per-stream metadata,
  including capability round-trip).
"""

from __future__ import annotations

import json
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import IO, Any, Optional

from syncfield.types import (
    ChirpSpec,
    FrameTimestamp,
    HealthEvent,
    SensorSample,
    SyncPoint,
)


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

    @property
    def path(self) -> Path:
        return self._path

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


class SessionLogWriter:
    """Writes orchestrator-level events (state transitions, health, rollbacks).

    One JSON object per line. Flushes on every write so logs survive a
    crash mid-recording and the core service can reconstruct partial
    sessions from the file.

    Output file: ``session_log.jsonl``
    """

    def __init__(self, output_dir: Path) -> None:
        self._path = output_dir / "session_log.jsonl"
        self._handle: IO[str] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def open(self) -> None:
        """Open the log file for writing. Idempotent on an already-open writer."""
        if self._handle is None:
            self._handle = open(self._path, "w")

    def log_event(self, event: dict[str, Any]) -> None:
        """Serialize *event* as a single JSON line and flush.

        Raises:
            RuntimeError: If the writer has not been opened.
        """
        if self._handle is None:
            raise RuntimeError("SessionLogWriter is not open")
        self._handle.write(json.dumps(event, separators=(",", ":")) + "\n")
        self._handle.flush()

    def log_health(self, event: HealthEvent) -> None:
        """Convenience wrapper that flattens a :class:`HealthEvent` to a log entry."""
        self.log_event(
            {
                "kind": "health",
                "stream_id": event.stream_id,
                "health_kind": event.kind.value,
                "at_ns": event.at_ns,
                "detail": event.detail,
            }
        )

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def write_sync_point(
    sync_point: SyncPoint,
    output_dir: Path,
    chirp_start_ns: Optional[int] = None,
    chirp_stop_ns: Optional[int] = None,
    chirp_spec: Optional[ChirpSpec] = None,
) -> Path:
    """Write ``sync_point.json`` to *output_dir* and return the path.

    Chirp-related fields are **omitted entirely** when ``None`` so single-host
    sessions and sessions configured with ``SyncToneConfig.silent()`` produce
    clean output that the sync core can ingest without special-casing.

    Args:
        sync_point: Captured session sync point.
        output_dir: Directory in which to write ``sync_point.json``.
        chirp_start_ns: Monotonic ns of the start chirp (if played), else None.
        chirp_stop_ns: Monotonic ns of the stop chirp (if played), else None.
        chirp_spec: Parameters of the chirp that was played, for reproducibility.

    Returns:
        Absolute path to the written file.
    """
    path = output_dir / "sync_point.json"
    data: dict[str, Any] = {"sdk_version": _pkg_version("syncfield")}
    data.update(sync_point.to_dict())
    if chirp_start_ns is not None:
        data["chirp_start_ns"] = chirp_start_ns
    if chirp_stop_ns is not None:
        data["chirp_stop_ns"] = chirp_stop_ns
    if chirp_spec is not None:
        data["chirp_spec"] = chirp_spec.to_dict()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return path


def write_manifest(
    host_id: str,
    streams: dict[str, dict[str, Any]],
    output_dir: Path,
) -> Path:
    """Write ``manifest.json`` to *output_dir* and return the path.

    The ``streams`` argument is written verbatim under the ``"streams"`` key,
    so callers may include any additional per-stream metadata — including
    ``"capabilities"`` dictionaries produced by
    :meth:`syncfield.types.StreamCapabilities.to_dict`.
    """
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
