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

from syncfield.health.types import IncidentSnapshot


# ---------------------------------------------------------------------------
# Aggregation snapshot
# ---------------------------------------------------------------------------


@dataclass
class AggregationSnapshot:
    """Mutable aggregation state surfaced into the viewer snapshot.

    Uses a plain (non-frozen) dataclass so the listener in server.py can
    update it in-place without reconstructing the SessionSnapshot.
    """

    active_job: Optional[Any] = None  # AggregationProgress; Any for optional-extra safety
    queue_length: int = 0
    recent_jobs: List[Any] = field(default_factory=list)


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
        latest_pose: The most recent *non-scalar* channel values in flat
            form (e.g. ``"hand_joints" -> [156 floats]``, ``"head_pose"
            -> [7 floats]`` from :class:`MetaQuestHandStream`). Unlike
            ``plot_points`` which tracks rolling scalar histories, this
            exposes the latest vector/list sample so panels that render
            a single-frame pose (3-D hand skeleton, quaternion axes…)
            have data to draw. Empty for streams that emit only scalars.
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
    latest_pose: Dict[str, List[float]]
    live_preview: bool = True
    connection_state: str = "idle"
    connection_error: Optional[str] = None


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
        chirp_mode: One of ``"ultrasound"``, ``"audible"``, ``"off"`` — the
            named preset the current :class:`SyncToneConfig` falls into.
        elapsed_s: Wall-clock seconds since ``start()``, or 0 if idle.
        streams: Ordered map ``stream_id -> StreamSnapshot``.
        active_incidents: Currently open incidents across all streams.
        resolved_incidents: Recently closed incidents (newest last, capped at 20).
    """

    host_id: str
    state: str
    output_dir: str
    sync_point_monotonic_ns: Optional[int]
    sync_point_wall_clock_ns: Optional[int]
    chirp_start_ns: Optional[int]
    chirp_stop_ns: Optional[int]
    chirp_enabled: bool
    chirp_mode: str = "off"
    elapsed_s: float = 0.0
    streams: Dict[str, StreamSnapshot] = field(default_factory=dict)
    active_incidents: List[IncidentSnapshot] = field(default_factory=list)
    resolved_incidents: List[IncidentSnapshot] = field(default_factory=list)
    aggregation: Optional[AggregationSnapshot] = None


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

    # Rolling fps window (monotonic ns)
    _fps_window: Deque[int] = field(default_factory=lambda: deque(maxlen=30))

    # Rolling plot buffers, one per numeric channel. Keys appear lazily.
    _plot_timestamps: Deque[float] = field(default_factory=lambda: deque(maxlen=300))
    _plot_channels: Dict[str, Deque[float]] = field(default_factory=dict)

    # Latest list/vector-valued channels (e.g. 156-float hand_joints
    # from MetaQuestHandStream). We only retain the most recent value
    # because the receiver — a 3-D pose panel — wants instantaneous
    # state, not a history, and the payload (~500 floats per pose) is
    # too large to buffer thousands of frames of.
    _latest_pose: Dict[str, List[float]] = field(default_factory=dict)

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

            # List/tuple-valued channels (e.g. MetaQuestHandStream's
            # hand_joints: 156 floats per sample) — retain only the
            # latest value. 3-D pose panels want an instantaneous
            # snapshot; keeping a rolling history would explode memory
            # (~500 floats × 300 samples × 1 adapter = 150k floats).
            for name, value in channels.items():
                if _is_auxiliary_channel(name):
                    continue
                if isinstance(value, (list, tuple)):
                    # Cast to a plain list of floats so the SSE JSON
                    # serializer never sees numpy scalars or similar.
                    try:
                        self._latest_pose[name] = [float(v) for v in value]
                    except (TypeError, ValueError):
                        # Best-effort: skip non-numeric list payloads.
                        continue

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

    def snapshot_fps(self, now_ns: int) -> float:
        """Effective Hz over the last second of samples.

        Computed from the time span between the oldest and newest
        samples in the 1-second window, giving fractional precision
        (e.g. 29.47 Hz instead of 29 or 30).
        """
        if not self._fps_window:
            return 0.0
        window_start = now_ns - 1_000_000_000
        recent = [t for t in self._fps_window if t >= window_start]
        if len(recent) < 2:
            return float(len(recent))
        span_s = (max(recent) - min(recent)) / 1e9
        if span_s <= 0:
            return 0.0
        return (len(recent) - 1) / span_s

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

    def snapshot_pose(self) -> Dict[str, List[float]]:
        """Return the most-recent list-valued channel samples.

        Returns a plain dict so the viewer render thread can serialise
        it straight to JSON without touching the poller's state.
        """
        return {name: list(values) for name, values in self._latest_pose.items()}


# ---------------------------------------------------------------------------
# Free helpers
# ---------------------------------------------------------------------------


def _is_auxiliary_channel(name: str) -> bool:
    """Return True for channel names that shouldn't appear in the plot.

    Adapters sometimes attach metadata channels alongside real sensor
    readings. Timestamp-like auxiliary values often sit in the nanoseconds
    range (~10^18) and, if plotted alongside real readings on a single
    auto-scaled Y axis, flatten every real reading into a straight baseline.

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
