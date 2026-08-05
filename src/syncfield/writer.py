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
import queue
import threading
import time
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


_SENSOR_WRITER_STOP = object()


class SensorWriter:
    """Writes ``SensorSample`` entries to a per-stream JSONL file.

    The default mode preserves the original synchronous contract: each call to
    :meth:`write` appends one JSON line and flushes immediately. High-rate
    adapters may opt into ``queue_capacity`` to move JSON encoding and batched
    flushes onto a dedicated writer thread. That keeps a slow filesystem from
    applying back-pressure to a hardware receive loop while leaving every
    existing caller unchanged.

    Output file: ``{stream_id}.jsonl``
    """

    def __init__(
        self,
        stream_id: str,
        output_dir: Path,
        *,
        queue_capacity: int = 0,
        batch_size: int = 1,
        flush_interval_s: float = 0.1,
    ) -> None:
        if queue_capacity < 0:
            raise ValueError("queue_capacity must be non-negative")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if flush_interval_s <= 0:
            raise ValueError("flush_interval_s must be positive")
        self._stream_id = stream_id
        self._path = output_dir / f"{stream_id}.jsonl"
        self._handle: IO[str] | None = None
        self._count = 0
        self._written_count = 0
        self._batch_count = 0
        self._enqueue_failures = 0
        self._queue_high_watermark = 0
        self._queue_capacity = queue_capacity
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._queue: queue.Queue[SensorSample | object] | None = (
            queue.Queue(maxsize=queue_capacity) if queue_capacity else None
        )
        self._worker: threading.Thread | None = None
        self._worker_error: BaseException | None = None
        self._closing = False
        self._metrics_lock = threading.Lock()

    @property
    def count(self) -> int:
        return self._count

    @property
    def path(self) -> Path:
        return self._path

    @property
    def buffered(self) -> bool:
        return self._queue is not None

    def open(self) -> None:
        if self._handle is not None:
            raise RuntimeError(f"SensorWriter for '{self._stream_id}' is already open")
        self._handle = open(self._path, "w")
        self._closing = False
        self._worker_error = None
        if self._queue is not None:
            self._worker = threading.Thread(
                target=self._run_buffered_writer,
                name=f"sensor-writer-{self._stream_id}",
                daemon=True,
            )
            self._worker.start()

    def write(self, sample: SensorSample) -> None:
        if self._handle is None:
            raise RuntimeError(f"SensorWriter for '{self._stream_id}' is not open")
        if self._closing:
            raise RuntimeError(f"SensorWriter for '{self._stream_id}' is closing")
        self._raise_worker_error()
        if self._queue is None:
            self._write_batch((sample,))
            with self._metrics_lock:
                self._count += 1
            return

        try:
            self._queue.put_nowait(sample)
        except queue.Full as exc:
            with self._metrics_lock:
                self._enqueue_failures += 1
            raise BufferError(
                f"SensorWriter queue for '{self._stream_id}' is full "
                f"({self._queue_capacity} samples)"
            ) from exc
        depth = self._queue.qsize()
        with self._metrics_lock:
            self._count += 1
            self._queue_high_watermark = max(self._queue_high_watermark, depth)

    def metrics_snapshot(self) -> dict[str, int]:
        """Return bounded live counters suitable for host telemetry."""

        with self._metrics_lock:
            count = self._count
            written = self._written_count
            batches = self._batch_count
            failures = self._enqueue_failures
            high_watermark = self._queue_high_watermark
        return {
            "queue_depth": self._queue.qsize() if self._queue is not None else 0,
            "queue_capacity": self._queue_capacity,
            "queue_high_watermark": high_watermark,
            "samples_enqueued_total": count,
            "samples_written_total": written,
            "batches_written_total": batches,
            "enqueue_failures_total": failures,
        }

    def _write_batch(self, samples: tuple[SensorSample, ...] | list[SensorSample]) -> None:
        handle = self._handle
        if handle is None:
            raise RuntimeError(f"SensorWriter for '{self._stream_id}' is not open")
        payload = "".join(
            json.dumps(sample.to_dict(), separators=(",", ":")) + "\n"
            for sample in samples
        )
        handle.write(payload)
        handle.flush()
        with self._metrics_lock:
            self._written_count += len(samples)
            self._batch_count += 1

    def _run_buffered_writer(self) -> None:
        assert self._queue is not None
        pending: list[SensorSample] = []
        deadline = time.monotonic() + self._flush_interval_s
        try:
            while True:
                timeout = max(0.0, deadline - time.monotonic())
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    item = None

                if item is _SENSOR_WRITER_STOP:
                    if pending:
                        self._write_batch(pending)
                    return
                if isinstance(item, SensorSample):
                    pending.append(item)

                now = time.monotonic()
                if pending and (
                    len(pending) >= self._batch_size or now >= deadline
                ):
                    self._write_batch(pending)
                    pending = []
                    deadline = now + self._flush_interval_s
                elif not pending and now >= deadline:
                    deadline = now + self._flush_interval_s
        except BaseException as exc:  # surfaced on the producer and close paths
            self._worker_error = exc

    def _raise_worker_error(self) -> None:
        if self._worker_error is not None:
            raise RuntimeError(
                f"SensorWriter worker for '{self._stream_id}' failed"
            ) from self._worker_error

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._closing = True
        worker = self._worker
        if self._queue is not None and worker is not None and worker.is_alive():
            while worker.is_alive():
                try:
                    self._queue.put(_SENSOR_WRITER_STOP, timeout=0.1)
                    break
                except queue.Full:
                    continue
            worker.join()
        error = self._worker_error
        try:
            handle.flush()
        finally:
            handle.close()
            self._handle = None
            self._worker = None
        if error is not None:
            raise RuntimeError(
                f"SensorWriter worker for '{self._stream_id}' failed"
            ) from error


class SessionLogWriter:
    """Writes orchestrator-level events (state transitions, health, rollbacks).

    One JSON object per line. Flushes on every write so logs survive a
    crash mid-recording and the core service can reconstruct partial
    sessions from the file.

    Output files:
    - ``session_log.jsonl`` — state transitions and health events
    - ``incidents.jsonl`` — incident lifecycle events
    """

    def __init__(self, output_dir: Path) -> None:
        self._path = output_dir / "session_log.jsonl"
        self._incidents_path = output_dir / "incidents.jsonl"
        self._handle: IO[str] | None = None
        self._incidents_handle: IO[str] | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def incidents_path(self) -> Path:
        return self._incidents_path

    def open(self) -> None:
        """Open the log files for writing. Idempotent on an already-open writer."""
        if self._handle is None:
            self._handle = open(self._path, "w")
        if self._incidents_handle is None:
            self._incidents_handle = open(self._incidents_path, "w")

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

    def log_incident(self, incident: Any) -> None:
        """Serialize an :class:`Incident` as a single JSON line and flush.

        Raises:
            RuntimeError: If the writer has not been opened.
        """
        if self._incidents_handle is None:
            raise RuntimeError("SessionLogWriter is not open")
        self._incidents_handle.write(json.dumps(incident.to_dict(), separators=(",", ":")) + "\n")
        self._incidents_handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self._incidents_handle is not None:
            self._incidents_handle.close()
            self._incidents_handle = None


def write_sync_point(
    sync_point: SyncPoint,
    output_dir: Path,
    chirp_start_ns: Optional[int] = None,
    chirp_stop_ns: Optional[int] = None,
    chirp_start_source: Optional[str] = None,
    chirp_stop_source: Optional[str] = None,
    chirp_spec: Optional[ChirpSpec] = None,
    session_id: Optional[str] = None,
    role: Optional[str] = None,
) -> Path:
    """Write ``sync_point.json`` to *output_dir* and return the path.

    All optional fields are **omitted entirely** when ``None`` so
    single-host sessions and sessions configured with
    :meth:`syncfield.tone.SyncToneConfig.silent` produce clean output
    that the sync core can ingest without special-casing missing keys.

    Args:
        sync_point: Captured session sync point.
        output_dir: Directory in which to write ``sync_point.json``.
        chirp_start_ns: Best-available monotonic ns for the start chirp
            (hardware if available, else software fallback).
        chirp_stop_ns: Best-available monotonic ns for the stop chirp.
        chirp_start_source: Provenance of ``chirp_start_ns`` — one of
            ``"hardware"``, ``"software_fallback"``, ``"silent"``.
        chirp_stop_source: Provenance of ``chirp_stop_ns``.
        chirp_spec: Parameters of the chirp that was played, for
            reproducibility.
        session_id: Multi-host session identifier (from
            :class:`LeaderRole` / :class:`FollowerRole`).
        role: ``"leader"`` or ``"follower"`` for multi-host sessions.

    Returns:
        Absolute path to the written file.
    """
    path = output_dir / "sync_point.json"
    data: dict[str, Any] = {"sdk_version": _pkg_version("syncfield")}
    data.update(sync_point.to_dict())
    if session_id is not None:
        data["session_id"] = session_id
    if role is not None:
        data["role"] = role
    if chirp_start_ns is not None:
        data["chirp_start_ns"] = chirp_start_ns
    if chirp_stop_ns is not None:
        data["chirp_stop_ns"] = chirp_stop_ns
    if chirp_start_source is not None:
        data["chirp_start_source"] = chirp_start_source
    if chirp_stop_source is not None:
        data["chirp_stop_source"] = chirp_stop_source
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
    *,
    session_id: Optional[str] = None,
    role: Optional[str] = None,
    leader_host_id: Optional[str] = None,
    task: Optional[str] = None,
    session_config: Optional[dict[str, Any]] = None,
) -> Path:
    """Write ``manifest.json`` to *output_dir* and return the path.

    The ``streams`` argument is written verbatim under the ``"streams"``
    key, so callers may include any additional per-stream metadata —
    including ``"capabilities"`` dictionaries produced by
    :meth:`syncfield.types.StreamCapabilities.to_dict`.

    Multi-host fields (``session_id``, ``role``, ``leader_host_id``)
    are omitted entirely for single-host sessions so the manifest stays
    clean of defaulted null fields.
    """
    path = output_dir / "manifest.json"
    manifest: dict[str, Any] = {
        "sdk_version": _pkg_version("syncfield"),
        "host_id": host_id,
        "streams": streams,
    }
    if session_id is not None:
        manifest["session_id"] = session_id
    if role is not None:
        manifest["role"] = role
    if leader_host_id is not None:
        manifest["leader_host_id"] = leader_host_id
    if task is not None:
        manifest["task"] = task
    if session_config is not None:
        manifest["session_config"] = session_config
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    return path
