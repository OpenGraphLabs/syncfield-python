"""SyncSession — the main user-facing class for timestamp capture."""

from __future__ import annotations

import time
import threading
from pathlib import Path

from syncfield.types import FrameTimestamp, SyncPoint
from syncfield.writer import StreamWriter, write_sync_point


class SyncSession:
    """Capture timestamps for multi-stream synchronization.

    Usage::

        session = SyncSession(host_id="rig_01", output_dir="./timestamps")
        session.start()

        # In your I/O loop — call stamp() immediately AFTER each read()
        frame = camera.read()
        session.stamp("cam_left", frame_number=i)

        session.stop()

    The session is **thread-safe**: ``stamp()`` can be called from multiple
    threads concurrently (e.g. one thread per device).

    Output files (written to *output_dir*)::

        sync_point.json
        cam_left.timestamps.jsonl
        cam_right.timestamps.jsonl
        ...
    """

    def __init__(self, host_id: str, output_dir: str | Path) -> None:
        self._host_id = host_id
        self._output_dir = Path(output_dir)
        self._sync_point: SyncPoint | None = None
        self._writers: dict[str, StreamWriter] = {}
        self._lock = threading.Lock()
        self._started = False

    @property
    def sync_point(self) -> SyncPoint | None:
        return self._sync_point

    def start(self) -> SyncPoint:
        """Begin a recording session.

        Captures a :class:`SyncPoint` and prepares the output directory.
        Must be called before :meth:`stamp`.

        Returns:
            The captured :class:`SyncPoint`.

        Raises:
            RuntimeError: If the session is already started.
        """
        if self._started:
            raise RuntimeError("Session already started")
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._sync_point = SyncPoint.create_now(self._host_id)
        self._started = True
        return self._sync_point

    def stamp(
        self,
        stream_id: str,
        frame_number: int,
        uncertainty_ns: int = 5_000_000,
    ) -> int:
        """Record a timestamp for one data packet.

        Call this **immediately after** your I/O read completes — before any
        processing — to minimise jitter.

        Args:
            stream_id: Identifier for the data stream (e.g. ``"cam_left"``).
            frame_number: Sequential index (0-based) within this stream.
            uncertainty_ns: Timing uncertainty estimate (default 5 ms).

        Returns:
            The captured ``time.monotonic_ns()`` value.

        Raises:
            RuntimeError: If :meth:`start` has not been called.
        """
        if not self._started:
            raise RuntimeError("Session not started — call start() first")

        capture_ns = time.monotonic_ns()

        ts = FrameTimestamp(
            frame_number=frame_number,
            capture_ns=capture_ns,
            clock_source="host_monotonic",
            clock_domain=self._host_id,
            uncertainty_ns=uncertainty_ns,
        )

        with self._lock:
            writer = self._writers.get(stream_id)
            if writer is None:
                writer = StreamWriter(stream_id, self._output_dir)
                writer.open()
                self._writers[stream_id] = writer
            writer.write(ts)

        return capture_ns

    def stop(self) -> dict[str, int]:
        """End the recording session.

        Closes all writers, writes ``sync_point.json``, and validates
        timestamp monotonicity.

        Returns:
            Mapping of ``{stream_id: frame_count}`` for all recorded streams.

        Raises:
            RuntimeError: If :meth:`start` has not been called.
        """
        if not self._started:
            raise RuntimeError("Session not started")

        counts: dict[str, int] = {}
        for stream_id, writer in self._writers.items():
            counts[stream_id] = writer.count
            writer.close()

        if self._sync_point is not None:
            write_sync_point(self._sync_point, self._output_dir)

        self._started = False
        return counts
