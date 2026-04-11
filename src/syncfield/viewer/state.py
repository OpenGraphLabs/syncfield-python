"""Immutable snapshots of session state, produced by the poller.

The viewer never touches a live :class:`~syncfield.SessionOrchestrator` from
the GUI render loop — instead, a background thread polls the session at a
fixed cadence and produces a :class:`SessionSnapshot`. The render loop then
reads the snapshot under a lock and updates widgets. Because snapshots are
frozen dataclasses of plain Python values, widgets never need to reason
about threading or about whether the session is mid-transition.

Thread safety model:

- Snapshots are constructed by the poller thread only.
- Snapshots are read by the render loop on the main thread only.
- The :class:`~syncfield.viewer.poller.SessionPoller` guards handoff with
  a single lock — readers always get the latest fully-built snapshot.
- Numpy video frames are published by reference (no copy) because texture
  uploads in DearPyGui copy the buffer internally.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Stream-level snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamSnapshot:
    """Per-stream state captured at one polling tick.

    Attributes:
        id: The stream identifier.
        kind: ``"video" | "audio" | "sensor" | "custom"``.
        provides_audio_track: Convenience flag (mirrors the capability) —
            used by the viewer to decide whether to show the audio-chirp
            indicator next to the card.
        produces_file: Whether this stream writes a file.
        frame_count: Total samples/frames produced since start.
        last_sample_at_ns: Monotonic ns of the most recent sample, or None.
        effective_hz: Measured frame rate over a short rolling window.
        latest_frame: Most recent BGR/RGB frame (numpy array) for video
            streams. ``None`` for non-video or when no frame has arrived yet.
        plot_points: For sensor streams, a dict of ``channel_name ->
            (x_list, y_list)`` rolling buffers of numeric values. Empty for
            video streams.
        health_count: Number of health events this stream has buffered so
            far. Useful for showing a red dot on degraded streams.
    """

    id: str
    kind: str
    provides_audio_track: bool
    produces_file: bool
    frame_count: int
    last_sample_at_ns: Optional[int]
    effective_hz: float
    latest_frame: Any  # numpy array or None — kept as Any so numpy is optional
    plot_points: Dict[str, Tuple[List[float], List[float]]]
    health_count: int


# ---------------------------------------------------------------------------
# Health event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthEntry:
    """A single health event surfaced by any stream.

    Simpler than :class:`~syncfield.types.HealthEvent` because the viewer
    only needs strings for display and a monotonic ordering key.
    """

    stream_id: str
    kind: str   # "heartbeat" | "drop" | "reconnect" | "warning" | "error"
    at_ns: int
    detail: Optional[str]


# ---------------------------------------------------------------------------
# Session-level snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionSnapshot:
    """Everything the viewer needs to render one frame.

    Attributes:
        host_id: Session host identifier.
        state: Lowercase state string (``"idle"``, ``"recording"``, ...).
        output_dir: Absolute path string for the session output directory.
        sync_point_monotonic_ns: Captured at ``session.start()``, or None
            before start.
        sync_point_wall_clock_ns: Wall-clock ns captured at start.
        chirp_start_ns: Monotonic ns when the start chirp was played.
        chirp_stop_ns: Monotonic ns when the stop chirp was played.
        chirp_enabled: Whether :class:`SyncToneConfig` had chirp enabled.
        elapsed_s: Wall-clock seconds since ``start()``, or 0 if idle.
        streams: Ordered map ``stream_id -> StreamSnapshot``.
        health_log: Most recent health events across all streams (newest last).
    """

    host_id: str
    state: str
    output_dir: str
    sync_point_monotonic_ns: Optional[int]
    sync_point_wall_clock_ns: Optional[int]
    chirp_start_ns: Optional[int]
    chirp_stop_ns: Optional[int]
    chirp_enabled: bool
    elapsed_s: float
    streams: Dict[str, StreamSnapshot]
    health_log: List[HealthEntry]


# ---------------------------------------------------------------------------
# Helper buffers owned by the poller (not part of the snapshot contract)
# ---------------------------------------------------------------------------


@dataclass
class StreamStatsBuffer:
    """Mutable running stats the poller maintains per stream.

    Lives inside the poller and gets snapshotted into immutable
    :class:`StreamSnapshot`\\ s on each tick. Kept separate from the
    snapshot so the render loop never sees mutable state.
    """

    max_plot_samples: int = 300
    max_health: int = 20

    # Rolling fps window (monotonic ns)
    _fps_window: Deque[int] = field(default_factory=lambda: deque(maxlen=30))

    # Rolling plot buffers, one per numeric channel. Keys appear lazily.
    _plot_timestamps: Deque[float] = field(default_factory=lambda: deque(maxlen=300))
    _plot_channels: Dict[str, Deque[float]] = field(default_factory=dict)

    # Health events produced by this stream (capped)
    _health: Deque[HealthEntry] = field(default_factory=lambda: deque(maxlen=20))

    def observe_sample(self, capture_ns: int, channels: Optional[Dict[str, Any]]) -> None:
        """Record one sample. Called from the stream's callback thread.

        Thread-safe against the poller's snapshot reader because all deques
        are bounded — truncation happens inside ``deque.append`` atomically
        and readers call :meth:`snapshot_fps` / :meth:`snapshot_plot` which
        make a list copy.

        Auxiliary channels — timestamps, metadata, anything whose name
        starts with ``_`` or contains ``timestamp`` — are skipped from
        the plot buffer. Those values often live in the nanoseconds
        range (~10¹⁸) and would otherwise dominate the auto-scaled Y
        axis, squashing real sensor readings (0–65535 for OGLO FSRs)
        flat against the baseline.
        """
        self._fps_window.append(capture_ns)
        self._plot_timestamps.append(capture_ns / 1e9)

        if channels:
            plottable = {
                name: value
                for name, value in channels.items()
                if isinstance(value, (int, float))
                and not _is_auxiliary_channel(name)
            }

            # Pad any missing channel to align lengths, then append numeric values.
            current_len = len(self._plot_timestamps)
            for name, value in plottable.items():
                buf = self._plot_channels.get(name)
                if buf is None:
                    buf = deque(maxlen=self.max_plot_samples)
                    # Back-fill with NaN so the x/y arrays line up in the plot.
                    padding = max(0, current_len - 1)
                    for _ in range(padding):
                        buf.append(float("nan"))
                    self._plot_channels[name] = buf
                buf.append(float(value))

            # Any channel we already track but that's missing from this sample
            # gets a NaN so it doesn't drift out of alignment.
            for name, buf in self._plot_channels.items():
                if name not in plottable:
                    buf.append(float("nan"))

    def observe_health(self, event: HealthEntry) -> None:
        self._health.append(event)

    def snapshot_fps(self, now_ns: int) -> float:
        """Effective Hz over the last second of samples, or 0 if no data."""
        if not self._fps_window:
            return 0.0
        window_start = now_ns - 1_000_000_000
        recent = [t for t in self._fps_window if t >= window_start]
        return float(len(recent))

    def snapshot_plot(self) -> Dict[str, Tuple[List[float], List[float]]]:
        """Copy plot buffers into plain lists safe for the render thread."""
        x = list(self._plot_timestamps)
        if not x:
            return {}
        out: Dict[str, Tuple[List[float], List[float]]] = {}
        for name, buf in self._plot_channels.items():
            y = list(buf)
            # Align: the channel's buffer may be shorter if it just appeared
            # mid-stream; left-pad with NaN to match x.
            if len(y) < len(x):
                y = [float("nan")] * (len(x) - len(y)) + y
            elif len(y) > len(x):
                y = y[-len(x):]
            out[name] = (x, y)
        return out

    def snapshot_health(self) -> List[HealthEntry]:
        return list(self._health)


# ---------------------------------------------------------------------------
# Free helpers
# ---------------------------------------------------------------------------


def _is_auxiliary_channel(name: str) -> bool:
    """Return True for channel names that shouldn't appear in the plot.

    Adapters sometimes attach metadata channels alongside real sensor
    readings — the OGLO tactile stream, for example, emits
    ``device_timestamp_ns`` next to its thumb/index/middle/ring/pinky
    FSR values so downstream consumers can recover the MCU hardware
    clock. Those auxiliary values often sit in the nanoseconds range
    (~10¹⁸) and, if plotted alongside the real 0–65535 readings on a
    single auto-scaled Y axis, flatten every real reading into a
    straight baseline.

    The rule is deliberately simple so third-party adapters can opt
    channels out of plotting by convention alone — without any API
    hook — by:

    * prefixing the channel name with an underscore (``_raw``,
      ``_calibration``, …), or
    * including the substring ``timestamp`` in the channel name
      (``device_timestamp_ns``, ``capture_timestamp_us``, …).
    """
    if not name:
        return False
    if name.startswith("_"):
        return True
    if "timestamp" in name.lower():
        return True
    return False
