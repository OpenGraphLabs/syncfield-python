"""SyncSession — the main user-facing class for timestamp capture."""

from __future__ import annotations

import time
import threading
from pathlib import Path
from typing import Any

from syncfield.types import FrameTimestamp, SensorSample, SyncPoint
from syncfield.writer import SensorWriter, StreamWriter, write_manifest, write_sync_point


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
        self._sensor_writers: dict[str, SensorWriter] = {}
        self._links: dict[str, str] = {}
        self._recorded_streams: set[str] = set()
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
        capture_ns: int | None = None,
    ) -> int:
        """Record a timestamp for one data packet.

        Call this **immediately after** your I/O read completes — before any
        processing — to minimise jitter.

        Args:
            stream_id: Identifier for the data stream (e.g. ``"cam_left"``).
            frame_number: Sequential index (0-based) within this stream.
            uncertainty_ns: Timing uncertainty estimate (default 5 ms).
            capture_ns: Pre-captured ``time.monotonic_ns()`` value. If
                ``None`` (default), the SDK captures it at call time.

        Returns:
            The ``time.monotonic_ns()`` value used for this timestamp.

        Raises:
            RuntimeError: If :meth:`start` has not been called.
        """
        if not self._started:
            raise RuntimeError("Session not started — call start() first")

        if capture_ns is None:
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

    def record(
        self,
        stream_id: str,
        frame_number: int,
        channels: dict[str, Any],
        uncertainty_ns: int = 5_000_000,
        capture_ns: int | None = None,
    ) -> int:
        """Record a sensor sample with timestamp and channel data.

        Captures ``time.monotonic_ns()``, then writes to both
        ``{stream_id}.timestamps.jsonl`` and ``{stream_id}.jsonl``.

        Channels can be flat (``{"accel_x": 0.12}``) or nested
        (``{"joints": {"wrist": [0.1, 0.2, 0.3]}}``).

        Args:
            stream_id: Identifier for the sensor stream (e.g. ``"imu"``).
            frame_number: Sequential index (0-based) within this stream.
            channels: Sensor data as ``{name: value}`` pairs. Values can be
                floats, lists, or nested dicts for complex sensors.
            uncertainty_ns: Timing uncertainty estimate (default 5 ms).
            capture_ns: Pre-captured ``time.monotonic_ns()`` value. If
                ``None`` (default), the SDK captures it at call time.

        Returns:
            The ``time.monotonic_ns()`` value used for this timestamp.

        Raises:
            RuntimeError: If :meth:`start` has not been called.
        """
        if not self._started:
            raise RuntimeError("Session not started — call start() first")

        if capture_ns is None:
            capture_ns = time.monotonic_ns()

        ts = FrameTimestamp(
            frame_number=frame_number,
            capture_ns=capture_ns,
            clock_source="host_monotonic",
            clock_domain=self._host_id,
            uncertainty_ns=uncertainty_ns,
        )

        sample = SensorSample(
            frame_number=frame_number,
            capture_ns=capture_ns,
            channels=channels,
            clock_source="host_monotonic",
            clock_domain=self._host_id,
            uncertainty_ns=uncertainty_ns,
        )

        with self._lock:
            # Timestamp writer
            ts_writer = self._writers.get(stream_id)
            if ts_writer is None:
                ts_writer = StreamWriter(stream_id, self._output_dir)
                ts_writer.open()
                self._writers[stream_id] = ts_writer
            ts_writer.write(ts)

            # Sensor data writer
            sensor_writer = self._sensor_writers.get(stream_id)
            if sensor_writer is None:
                sensor_writer = SensorWriter(stream_id, self._output_dir)
                sensor_writer.open()
                self._sensor_writers[stream_id] = sensor_writer
            sensor_writer.write(sample)

            self._recorded_streams.add(stream_id)

        return capture_ns

    def link(self, stream_id: str, path: str | Path) -> None:
        """Associate an external file path with a stream.

        Use this for files produced outside the SDK (e.g. video files,
        pre-converted sensor files). The association is recorded in
        ``manifest.json`` when :meth:`stop` is called.

        Args:
            stream_id: The stream identifier.
            path: Path to the external file.
        """
        self._links[stream_id] = str(path)

    def stop(self) -> dict[str, int]:
        """End the recording session.

        Closes all writers and writes ``sync_point.json`` and
        ``manifest.json``.

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

        for writer in self._sensor_writers.values():
            writer.close()

        if self._sync_point is not None:
            write_sync_point(self._sync_point, self._output_dir)

        # Build manifest
        streams: dict[str, dict[str, Any]] = {}
        all_ids = sorted(
            set(self._writers) | set(self._links) | self._recorded_streams,
        )
        for stream_id in all_ids:
            entry: dict[str, Any] = {}

            if stream_id in self._recorded_streams:
                entry["type"] = "sensor"
                entry["sensor_path"] = f"{stream_id}.jsonl"
            else:
                entry["type"] = "video"

            if stream_id in self._writers:
                entry["timestamps_path"] = f"{stream_id}.timestamps.jsonl"
                entry["frame_count"] = counts[stream_id]

            if stream_id in self._links:
                entry["path"] = self._links[stream_id]

            streams[stream_id] = entry

        write_manifest(self._host_id, streams, self._output_dir)

        self._started = False
        return counts
