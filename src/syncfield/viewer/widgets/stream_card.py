"""Per-stream card widgets.

Each stream is rendered as a 260 x 300 card. The card's body varies by
stream kind:

- **video** → live GPU texture fed from ``stream.latest_frame``
- **sensor** → a small line plot of numeric channels (up to 6 series)
- everything else → a minimal stats block

The card shell, header, and stats row are identical across variants so the
viewer looks consistent regardless of what kind of data the user registers.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

import dearpygui.dearpygui as dpg
import numpy as np

from syncfield.viewer import theme
from syncfield.viewer.fonts import FontRegistry
from syncfield.viewer.state import StreamSnapshot
from syncfield.viewer.widgets.formatting import (
    format_count,
    format_hz,
    format_ns_ago,
)

#: Session states in which a stream may be removed from the live session.
#: Kept in sync with :meth:`syncfield.orchestrator.SessionOrchestrator.remove`
#: — any state outside this set disables the remove button on every card.
_REMOVABLE_STATES = frozenset({"idle", "connected", "stopped"})


# Texture resolution for video previews. We keep this fixed so all cards
# share one preset; real frames are resized (with aspect-ratio letterboxing)
# into this buffer before upload.
PREVIEW_W = 260
PREVIEW_H = theme.VIDEO_THUMBNAIL_HEIGHT


class StreamCard:
    """Owns the DearPyGui nodes for one stream card.

    One instance per registered stream. Construction happens lazily the
    first time the layout sees a given stream id in a snapshot, so dynamic
    stream additions Just Work.
    """

    def __init__(
        self,
        parent_tag: str,
        snapshot: StreamSnapshot,
        *,
        fonts: Optional[FontRegistry] = None,
        on_remove: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._stream_id = snapshot.id
        self._kind = snapshot.kind
        self._fonts = fonts or FontRegistry()
        self._on_remove = on_remove
        self._card_tag = f"card::{snapshot.id}"
        self._title_tag = f"card_title::{snapshot.id}"
        self._state_dot_tag = f"card_dot::{snapshot.id}"
        self._frame_count_tag = f"card_frames::{snapshot.id}"
        self._hz_tag = f"card_hz::{snapshot.id}"
        self._last_sample_tag = f"card_last::{snapshot.id}"
        self._capability_tag = f"card_cap::{snapshot.id}"
        self._remove_button_tag = f"card_remove::{snapshot.id}"
        self._last_remove_enabled: Optional[bool] = None

        # Variant-specific tags (populated by the matching _build_body method)
        self._texture_tag: Optional[str] = None
        self._plot_tag: Optional[str] = None
        self._plot_x_axis_tag: Optional[str] = None
        self._plot_y_axis_tag: Optional[str] = None
        self._series_tags: Dict[str, str] = {}

        self._build(parent_tag, snapshot)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self, parent_tag: str, snapshot: StreamSnapshot) -> None:
        with dpg.child_window(
            tag=self._card_tag,
            parent=parent_tag,
            width=theme.CARD_WIDTH,
            height=theme.CARD_HEIGHT,
            border=True,
            no_scrollbar=True,
        ):
            dpg.bind_item_theme(self._card_tag, theme.build_card_theme())

            # --- Header row: stream id + status dot + remove button ---
            #
            # We right-pin the ``×`` button by pre-computing the spacer
            # width from the card width. DPG has no flexbox; a fixed
            # spacer is the simplest way to keep the remove button
            # anchored to the card's top-right corner regardless of the
            # stream id's length.
            _REMOVE_BUTTON_W = 22
            _CONTENT_PADDING = 14   # DPG child_window inner padding
            _HEADER_GAP = 6
            with dpg.group(horizontal=True):
                dpg.add_text(snapshot.id, tag=self._title_tag)
                dpg.add_spacer(width=_HEADER_GAP)
                dpg.add_text(
                    "●",
                    tag=self._state_dot_tag,
                    color=theme.SUCCESS,
                )
                # Push the × to the right edge. We assume the id fits
                # in the default header width; longer ids bleed into
                # the spacer first before clipping the button.
                spacer_w = max(
                    4,
                    theme.CARD_WIDTH
                    - _CONTENT_PADDING * 2
                    - _REMOVE_BUTTON_W
                    - 60,  # rough width budget for id text + dot
                )
                dpg.add_spacer(width=spacer_w)
                dpg.add_button(
                    label="×",
                    tag=self._remove_button_tag,
                    width=_REMOVE_BUTTON_W,
                    height=_REMOVE_BUTTON_W,
                    callback=self._on_remove_click,
                )
                dpg.bind_item_theme(
                    self._remove_button_tag,
                    theme.build_ghost_button_theme(),
                )
            dpg.add_text(
                _capability_label(snapshot),
                tag=self._capability_tag,
                color=theme.TEXT_MUTED,
            )
            dpg.add_spacer(height=8)

            # Bind card title to the emphasized font once the tag exists.
            if self._fonts.ui_md is not None:
                try:
                    dpg.bind_item_font(self._title_tag, self._fonts.ui_md)
                except Exception:
                    pass
            if self._fonts.ui_sm is not None:
                try:
                    dpg.bind_item_font(self._capability_tag, self._fonts.ui_sm)
                except Exception:
                    pass

            # --- Body: variant-specific --------------------------------
            if self._kind == "video":
                self._build_video_body(snapshot)
            elif self._kind in ("sensor", "audio"):
                self._build_plot_body(snapshot)
            else:
                self._build_stats_body()

            dpg.add_spacer(height=8)

            # --- Footer stats row -------------------------------------
            with dpg.group(horizontal=True):
                dpg.add_text(
                    format_count(snapshot.frame_count),
                    tag=self._frame_count_tag,
                    color=theme.TEXT_PRIMARY,
                )
                dpg.add_spacer(width=4)
                dpg.add_text("frames", color=theme.TEXT_SECONDARY)
                dpg.add_spacer(width=14)
                dpg.add_text(
                    format_hz(snapshot.effective_hz),
                    tag=self._hz_tag,
                    color=theme.TEXT_SECONDARY,
                )
            dpg.add_text(
                "last sample: —",
                tag=self._last_sample_tag,
                color=theme.TEXT_MUTED,
            )

            # Stats row uses monospace so numeric counters don't jitter.
            if self._fonts.mono is not None:
                try:
                    dpg.bind_item_font(self._frame_count_tag, self._fonts.mono)
                    dpg.bind_item_font(self._hz_tag, self._fonts.mono)
                except Exception:
                    pass
            if self._fonts.ui_sm is not None:
                try:
                    dpg.bind_item_font(self._last_sample_tag, self._fonts.ui_sm)
                except Exception:
                    pass

    def _build_video_body(self, snapshot: StreamSnapshot) -> None:
        """A raw-texture image that the render loop updates in place."""
        self._texture_tag = f"texture::{snapshot.id}"
        initial = np.zeros(PREVIEW_W * PREVIEW_H * 4, dtype=np.float32)
        with dpg.texture_registry(show=False):
            dpg.add_raw_texture(
                width=PREVIEW_W,
                height=PREVIEW_H,
                default_value=initial,
                format=dpg.mvFormat_Float_rgba,
                tag=self._texture_tag,
            )
        dpg.add_image(
            self._texture_tag,
            width=PREVIEW_W - 28,  # account for card padding
            height=PREVIEW_H,
        )

    def _build_plot_body(self, snapshot: StreamSnapshot) -> None:
        """A line plot for numeric sensor channels."""
        self._plot_tag = f"plot::{snapshot.id}"
        self._plot_x_axis_tag = f"plot_x::{snapshot.id}"
        self._plot_y_axis_tag = f"plot_y::{snapshot.id}"
        with dpg.plot(
            tag=self._plot_tag,
            height=theme.PLOT_HEIGHT,
            width=-1,
            no_title=True,
            no_menus=True,
            no_mouse_pos=True,
        ):
            dpg.add_plot_axis(
                dpg.mvXAxis, tag=self._plot_x_axis_tag, no_tick_labels=True
            )
            dpg.add_plot_axis(
                dpg.mvYAxis, tag=self._plot_y_axis_tag, no_tick_labels=True
            )

    def _build_stats_body(self) -> None:
        """Fallback body — a discreet placeholder for custom/opaque streams."""
        with dpg.group():
            dpg.add_text(
                "no live preview",
                color=theme.TEXT_MUTED,
            )
            dpg.add_spacer(height=theme.VIDEO_THUMBNAIL_HEIGHT - 24)

    # ------------------------------------------------------------------
    # Update — called every render frame
    # ------------------------------------------------------------------

    def update(
        self,
        snapshot: StreamSnapshot,
        now_ns: int,
        session_state: Optional[str] = None,
    ) -> None:
        """Sync this card to the newest snapshot.

        Args:
            snapshot: Latest per-stream data.
            now_ns: Monotonic ns for staleness comparisons.
            session_state: Lowercase session state string (see
                :attr:`SessionSnapshot.state`). Used to enable or
                disable the remove button — removal is only legal in
                ``idle`` / ``connected`` / ``stopped``. ``None`` means
                "don't touch the button" (the initial state set at
                build time).
        """
        dpg.set_value(self._frame_count_tag, format_count(snapshot.frame_count))
        dpg.set_value(self._hz_tag, format_hz(snapshot.effective_hz))
        dpg.set_value(
            self._last_sample_tag,
            f"last sample: {format_ns_ago(snapshot.last_sample_at_ns, now_ns)}",
        )
        dpg.configure_item(
            self._state_dot_tag,
            color=_dot_color(snapshot, now_ns),
        )

        if session_state is not None:
            self._sync_remove_button_enabled(session_state)

        if self._kind == "video":
            self._update_video_texture(snapshot)
        elif self._kind in ("sensor", "audio"):
            self._update_plot(snapshot)

    def _sync_remove_button_enabled(self, session_state: str) -> None:
        """Enable the remove button only in removal-safe session states.

        Caches the last enabled/disabled state so DPG doesn't get a
        fresh ``configure_item`` call every render frame — the check
        is a cheap string membership test + equality.
        """
        should_enable = session_state in _REMOVABLE_STATES
        if should_enable == self._last_remove_enabled:
            return
        try:
            if should_enable:
                dpg.enable_item(self._remove_button_tag)
            else:
                dpg.disable_item(self._remove_button_tag)
        except Exception:  # pragma: no cover — DPG not yet ready at first tick
            return
        self._last_remove_enabled = should_enable

    def _on_remove_click(self, sender=None, app_data=None, user_data=None) -> None:
        """Fire the injected remove callback with this card's stream id.

        The callback (owned by :class:`ViewerLayout`) is expected to
        call :meth:`SessionOrchestrator.remove` on a worker thread so
        the UI thread doesn't block on device teardown.
        """
        if self._on_remove is not None:
            self._on_remove(self._stream_id)

    def _update_video_texture(self, snapshot: StreamSnapshot) -> None:
        """Upload the latest frame to the GPU texture, with letterboxing."""
        if self._texture_tag is None:
            return
        frame = snapshot.latest_frame
        if frame is None:
            return
        try:
            rgba = _fit_to_preview_rgba(frame, PREVIEW_W, PREVIEW_H)
        except Exception:
            # A single frame with an unexpected shape should never tear
            # the whole card down.
            return
        dpg.set_value(self._texture_tag, rgba)

    def _update_plot(self, snapshot: StreamSnapshot) -> None:
        """Update or create per-channel line series."""
        if self._plot_tag is None or self._plot_y_axis_tag is None:
            return
        for index, (channel_name, (xs, ys)) in enumerate(snapshot.plot_points.items()):
            series_tag = self._series_tags.get(channel_name)
            if series_tag is None:
                series_tag = f"series::{self._stream_id}::{channel_name}"
                dpg.add_line_series(
                    list(xs),
                    list(ys),
                    label=channel_name,
                    parent=self._plot_y_axis_tag,
                    tag=series_tag,
                )
                # Apply a per-series color so multi-channel plots stay legible.
                with dpg.theme() as series_theme:
                    with dpg.theme_component(dpg.mvLineSeries):
                        dpg.add_theme_color(
                            dpg.mvPlotCol_Line,
                            theme.series_color(index),
                            category=dpg.mvThemeCat_Plots,
                        )
                        dpg.add_theme_style(
                            dpg.mvPlotStyleVar_LineWeight,
                            1.8,
                            category=dpg.mvThemeCat_Plots,
                        )
                dpg.bind_item_theme(series_tag, series_theme)
                self._series_tags[channel_name] = series_tag
            else:
                dpg.set_value(series_tag, [list(xs), list(ys)])

        if snapshot.plot_points:
            dpg.fit_axis_data(self._plot_x_axis_tag)  # type: ignore[arg-type]
            dpg.fit_axis_data(self._plot_y_axis_tag)


# ---------------------------------------------------------------------------
# Free helpers
# ---------------------------------------------------------------------------


def _capability_label(snapshot: StreamSnapshot) -> str:
    tags: List[str] = [snapshot.kind]
    if snapshot.provides_audio_track:
        tags.append("audio")
    if snapshot.produces_file:
        tags.append("file")
    return " · ".join(tags)


def _dot_color(snapshot: StreamSnapshot, now_ns: int):
    """Pick a dot color based on freshness and health counts."""
    if snapshot.health_count > 0:
        return theme.WARNING
    last = snapshot.last_sample_at_ns
    if last is None:
        return theme.TEXT_MUTED
    if now_ns - last > 1_500_000_000:  # 1.5s stale
        return theme.WARNING
    return theme.SUCCESS


def _fit_to_preview_rgba(frame: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    """Convert an arbitrary BGR/RGB frame into a letterboxed RGBA float32 buffer.

    Uses simple NumPy slicing instead of cv2 so the viewer doesn't require
    opencv-python. That lets users install ``syncfield[viewer]`` without
    also needing the ``uvc`` extra.
    """
    if frame.ndim != 3 or frame.shape[2] < 3:
        raise ValueError(f"unexpected frame shape {frame.shape}")

    src_h, src_w = frame.shape[0], frame.shape[1]
    if src_h == 0 or src_w == 0:
        raise ValueError("empty frame")

    # Fit-within (letterbox) into target while preserving aspect ratio.
    scale = min(target_w / src_w, target_h / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))

    # Nearest-neighbor resize — cheap, no external deps. The viewer only
    # needs a thumbnail; interpolation quality isn't critical.
    ys = (np.linspace(0, src_h - 1, new_h)).astype(np.int32)
    xs = (np.linspace(0, src_w - 1, new_w)).astype(np.int32)
    resized = frame[ys][:, xs]

    # OAK frames come out as BGR (DepthAI) and so do UVC frames (OpenCV).
    # Swap to RGB so the preview colors match reality.
    if resized.shape[2] >= 3:
        resized = resized[:, :, [2, 1, 0]]

    # Letterbox into the full target buffer.
    canvas = np.full(
        (target_h, target_w, 3),
        fill_value=240,  # near-white letterbox matches the light theme
        dtype=np.uint8,
    )
    y_off = (target_h - new_h) // 2
    x_off = (target_w - new_w) // 2
    canvas[y_off : y_off + new_h, x_off : x_off + new_w] = resized[:, :, :3]

    # Convert to RGBA float32 in [0, 1] — DPG mvFormat_Float_rgba.
    rgba = np.empty((target_h, target_w, 4), dtype=np.float32)
    rgba[:, :, :3] = canvas.astype(np.float32) / 255.0
    rgba[:, :, 3] = 1.0
    return rgba.flatten()
