"""Top-level viewer layout.

One :class:`ViewerLayout` instance owns all the DearPyGui tags for the
viewer window. It builds the UI once in :meth:`build`, then every render
frame the app calls :meth:`update` with the latest
:class:`SessionSnapshot` and the layout fans the values out to each
widget. Stream cards are created lazily as new stream ids appear in
snapshots.

Sections (top to bottom):

    ┌── Header ──────────────────────────────── state · timer ─┐
    ├── Control panel  │  Session clock + chirp ──────────────┤
    ├── Streams (horizontal card row) ─────────────────────────┤
    ├── Health timeline ───────────────────────────────────────┤
    └── Footer: output dir · sync point wall clock ────────────┘
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional, TYPE_CHECKING

import dearpygui.dearpygui as dpg

from syncfield.orchestrator import SessionOrchestrator
from syncfield.types import SessionState
from syncfield.viewer import theme
from syncfield.viewer.state import SessionSnapshot
from syncfield.viewer.widgets.discovery_modal import DiscoveryModal
from syncfield.viewer.widgets.formatting import (
    format_chirp_pair,
    format_elapsed,
    format_path_tail,
    state_label,
)
from syncfield.viewer.widgets.stream_card import StreamCard


class ViewerLayout:
    """Owns the DearPyGui nodes for every section of the viewer.

    The layout does **not** import :class:`~syncfield.viewer.app.ViewerApp`
    directly — instead, callbacks capture the session and call its methods
    from a worker thread so the render loop never blocks.
    """

    def __init__(self, session: SessionOrchestrator) -> None:
        self._session = session
        self._cards: Dict[str, StreamCard] = {}
        self._streams_row_tag = "streams_row"
        self._health_table_tag = "health_table"
        self._last_health_keys: tuple = ()
        # Discovery modal — built lazily the first time the user clicks
        # the header button. Holds its own DPG tags so the layout does
        # not need to know about its internals.
        self._discovery_modal: Optional[DiscoveryModal] = None

    # ------------------------------------------------------------------
    # Build (called once at viewer startup)
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Construct the main window and all static chrome."""
        with dpg.window(
            tag="main_window",
            no_title_bar=True,
            no_move=True,
            no_resize=True,
            no_collapse=True,
            no_bring_to_front_on_focus=True,
        ):
            self._build_header()
            dpg.add_spacer(height=8)
            self._build_control_and_clock_row()
            dpg.add_spacer(height=8)
            self._build_streams_section()
            dpg.add_spacer(height=8)
            self._build_health_section()
            dpg.add_spacer(height=8)
            self._build_footer()

        # Button themes need the context to exist, so build them now.
        self._primary_theme = theme.build_primary_button_theme()
        self._danger_theme = theme.build_danger_button_theme()
        self._ghost_theme = theme.build_ghost_button_theme()
        self._soft_panel_theme = theme.build_soft_panel_theme()

        dpg.bind_item_theme("control_panel", self._soft_panel_theme)
        dpg.bind_item_theme("clock_panel", self._soft_panel_theme)
        dpg.bind_item_theme("btn_record", self._primary_theme)
        dpg.bind_item_theme("btn_stop", self._danger_theme)
        dpg.bind_item_theme("btn_cancel", self._ghost_theme)
        dpg.bind_item_theme("btn_discover", self._ghost_theme)

        # Construct (but don't yet show) the discovery modal. Building
        # it here means the first click on the Discover button opens
        # an already-ready window instead of waiting for DPG to build
        # on demand.
        self._discovery_modal = DiscoveryModal(self._session)
        self._discovery_modal.build()

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------

    def _build_header(self) -> None:
        """Top row: logo, host id, state chip, elapsed timer, discover button."""
        with dpg.group(horizontal=True):
            dpg.add_text("SyncField", tag="app_title")
            dpg.add_spacer(width=12)
            dpg.add_text("—", color=theme.TEXT_MUTED)
            dpg.add_spacer(width=12)
            dpg.add_text(self._session.host_id, tag="host_id_text")
            dpg.add_spacer(width=20)
            dpg.add_text("●", tag="state_dot", color=theme.STATE_IDLE)
            dpg.add_spacer(width=4)
            dpg.add_text("IDLE", tag="state_label", color=theme.TEXT_SECONDARY)
            dpg.add_spacer(width=20)
            dpg.add_text(
                "00:00.000",
                tag="elapsed_text",
                color=theme.TEXT_SECONDARY,
            )
            # Right-side spacer pushes the discover button to the edge.
            dpg.add_spacer(width=220)
            dpg.add_button(
                label="⚡  Discover devices",
                tag="btn_discover",
                width=180,
                height=30,
                callback=self._on_discover_click,
            )

        dpg.add_spacer(height=4)
        dpg.add_text(
            "Capture orchestration — live session view",
            color=theme.TEXT_MUTED,
        )

    def _build_control_and_clock_row(self) -> None:
        """Two side-by-side panels: controls + session clock."""
        with dpg.group(horizontal=True):
            # --- Control panel ----------------------------------------
            with dpg.child_window(
                tag="control_panel",
                width=260,
                height=theme.CONTROL_PANEL_HEIGHT,
                border=False,
                no_scrollbar=True,
            ):
                dpg.add_text("CONTROLS", color=theme.TEXT_MUTED)
                dpg.add_spacer(height=6)
                with dpg.group(horizontal=True):
                    dpg.add_button(
                        label="● Record",
                        tag="btn_record",
                        width=110,
                        height=34,
                        callback=self._on_record_click,
                    )
                    dpg.add_button(
                        label="■ Stop",
                        tag="btn_stop",
                        width=90,
                        height=34,
                        callback=self._on_stop_click,
                    )
                dpg.add_spacer(height=6)
                dpg.add_button(
                    label="Cancel",
                    tag="btn_cancel",
                    width=206,
                    height=28,
                    callback=self._on_cancel_click,
                )

            dpg.add_spacer(width=12)

            # --- Session clock + chirp panel --------------------------
            with dpg.child_window(
                tag="clock_panel",
                width=-1,
                height=theme.CONTROL_PANEL_HEIGHT,
                border=False,
                no_scrollbar=True,
            ):
                dpg.add_text("SESSION CLOCK", color=theme.TEXT_MUTED)
                dpg.add_spacer(height=6)
                with dpg.group(horizontal=True):
                    dpg.add_text("sync_point", color=theme.TEXT_SECONDARY)
                    dpg.add_spacer(width=8)
                    dpg.add_text("—", tag="sync_point_text")
                with dpg.group(horizontal=True):
                    dpg.add_text("chirp", color=theme.TEXT_SECONDARY)
                    dpg.add_spacer(width=38)
                    dpg.add_text("pending", tag="chirp_text")
                with dpg.group(horizontal=True):
                    dpg.add_text("tone", color=theme.TEXT_SECONDARY)
                    dpg.add_spacer(width=42)
                    dpg.add_text("—", tag="tone_text")

    def _build_streams_section(self) -> None:
        """Horizontal scrollable row of stream cards."""
        dpg.add_text("STREAMS", color=theme.TEXT_MUTED)
        dpg.add_spacer(height=4)
        with dpg.child_window(
            tag="streams_container",
            width=-1,
            height=theme.STREAMS_SECTION_HEIGHT,
            border=False,
            horizontal_scrollbar=True,
        ):
            with dpg.group(horizontal=True, tag=self._streams_row_tag):
                pass  # Cards added lazily in update()

    def _build_health_section(self) -> None:
        """A table of recent health events."""
        dpg.add_text("HEALTH EVENTS", color=theme.TEXT_MUTED)
        dpg.add_spacer(height=4)
        with dpg.child_window(
            width=-1,
            height=theme.HEALTH_SECTION_HEIGHT,
            border=False,
            no_scrollbar=False,
        ):
            with dpg.table(
                tag=self._health_table_tag,
                header_row=True,
                borders_innerH=True,
                borders_outerH=False,
                borders_innerV=False,
                borders_outerV=False,
                row_background=True,
                scrollY=True,
                height=theme.HEALTH_SECTION_HEIGHT - 24,
            ):
                dpg.add_table_column(label="Time", width_fixed=True, init_width_or_weight=90)
                dpg.add_table_column(label="Stream", width_fixed=True, init_width_or_weight=140)
                dpg.add_table_column(label="Kind", width_fixed=True, init_width_or_weight=120)
                dpg.add_table_column(label="Detail")

    def _build_footer(self) -> None:
        """Output path and wall clock."""
        with dpg.group(horizontal=True):
            dpg.add_text("output", color=theme.TEXT_MUTED)
            dpg.add_spacer(width=8)
            dpg.add_text("—", tag="output_text", color=theme.TEXT_SECONDARY)
        with dpg.group(horizontal=True):
            dpg.add_text("wall clock", color=theme.TEXT_MUTED)
            dpg.add_spacer(width=8)
            dpg.add_text("—", tag="wall_clock_text", color=theme.TEXT_SECONDARY)

    # ------------------------------------------------------------------
    # Update (called every render frame)
    # ------------------------------------------------------------------

    def update(self, snapshot: SessionSnapshot) -> None:
        """Sync every widget from the latest snapshot."""
        now_ns = time.monotonic_ns()

        self._update_header(snapshot)
        self._update_clock_panel(snapshot)
        self._update_controls(snapshot)
        self._update_streams(snapshot, now_ns)
        self._update_health(snapshot)
        self._update_footer(snapshot)

        # Discovery modal has its own per-frame tick that only does work
        # when the worker thread has produced new scan results or the
        # elapsed-time display needs a bump. Cheap no-op when closed.
        if self._discovery_modal is not None:
            self._discovery_modal.tick()

    def _update_header(self, snapshot: SessionSnapshot) -> None:
        dpg.configure_item("state_dot", color=theme.state_color(snapshot.state))
        dpg.set_value("state_label", state_label(snapshot.state))
        dpg.set_value("elapsed_text", format_elapsed(snapshot.elapsed_s))
        dpg.set_value("host_id_text", snapshot.host_id)

    def _update_clock_panel(self, snapshot: SessionSnapshot) -> None:
        if snapshot.sync_point_monotonic_ns is not None:
            sp_s = snapshot.sync_point_monotonic_ns / 1e9
            dpg.set_value("sync_point_text", f"{sp_s:,.3f}s  (monotonic)")
        else:
            dpg.set_value("sync_point_text", "—")

        dpg.set_value(
            "chirp_text",
            format_chirp_pair(snapshot.chirp_start_ns, snapshot.chirp_stop_ns)
            if snapshot.chirp_enabled
            else "disabled (silent)",
        )
        dpg.set_value(
            "tone_text",
            "400 → 2500 Hz, 500 ms" if snapshot.chirp_enabled else "—",
        )

    def _update_controls(self, snapshot: SessionSnapshot) -> None:
        state = snapshot.state
        # Enable/disable the three buttons based on the orchestrator state.
        _set_enabled("btn_record", state == "idle")
        _set_enabled("btn_stop", state == "recording")
        _set_enabled("btn_cancel", state in ("preparing", "recording"))
        # Discovery only makes sense before recording — the session's
        # ``add()`` contract refuses new streams once ``start()`` has
        # been called.
        _set_enabled("btn_discover", state == "idle")

    def _update_streams(self, snapshot: SessionSnapshot, now_ns: int) -> None:
        # Create cards for new streams.
        for stream_id, stream_snap in snapshot.streams.items():
            if stream_id not in self._cards:
                self._cards[stream_id] = StreamCard(self._streams_row_tag, stream_snap)
            self._cards[stream_id].update(stream_snap, now_ns)

        # Cards for streams that were removed (rare, but keep the UI honest).
        removed = set(self._cards.keys()) - set(snapshot.streams.keys())
        for stream_id in removed:
            card = self._cards.pop(stream_id)
            try:
                dpg.delete_item(card._card_tag)
            except Exception:
                pass

    def _update_health(self, snapshot: SessionSnapshot) -> None:
        """Rebuild the health table when the event set changes.

        We avoid rebuilding every frame — instead compare a cheap key
        (tuple of event at_ns + kind) and only touch DPG when the log
        actually changes. This keeps the table scroll position stable
        and avoids rapid row churn.
        """
        key = tuple((ev.at_ns, ev.kind, ev.stream_id) for ev in snapshot.health_log)
        if key == self._last_health_keys:
            return
        self._last_health_keys = key

        # Drop existing rows.
        for child in dpg.get_item_children(self._health_table_tag, 1) or []:
            dpg.delete_item(child)

        # Re-populate newest first.
        for ev in reversed(snapshot.health_log):
            with dpg.table_row(parent=self._health_table_tag):
                dpg.add_text(_format_time_short(ev.at_ns), color=theme.TEXT_SECONDARY)
                dpg.add_text(ev.stream_id)
                dpg.add_text(
                    ev.kind.upper(),
                    color=_health_kind_color(ev.kind),
                )
                dpg.add_text(ev.detail or "")

    def _update_footer(self, snapshot: SessionSnapshot) -> None:
        dpg.set_value("output_text", format_path_tail(snapshot.output_dir))
        if snapshot.sync_point_wall_clock_ns is not None:
            t = time.localtime(snapshot.sync_point_wall_clock_ns / 1e9)
            dpg.set_value(
                "wall_clock_text",
                time.strftime("%Y-%m-%d %H:%M:%S", t),
            )
        else:
            dpg.set_value("wall_clock_text", "—")

    # ------------------------------------------------------------------
    # Button callbacks — all delegate to a worker thread so the UI stays
    # responsive while the SDK's start()/stop() run.
    # ------------------------------------------------------------------

    def _on_record_click(self) -> None:
        threading.Thread(
            target=self._safe_call,
            args=(self._session.start,),
            name="viewer-ctrl-start",
            daemon=True,
        ).start()

    def _on_stop_click(self) -> None:
        threading.Thread(
            target=self._safe_call,
            args=(self._session.stop,),
            name="viewer-ctrl-stop",
            daemon=True,
        ).start()

    def _on_cancel_click(self) -> None:
        """Cancel is SessionOrchestrator.stop() if recording — the SDK
        has no dedicated cancel primitive, so we call stop() which takes
        the best-effort path. Applications with richer cancellation can
        subclass this layout in the future."""
        self._on_stop_click()

    def _on_discover_click(self) -> None:
        """Open the discovery modal. Disabled while recording to keep the
        registry-add path out of a live session's hot path."""
        if self._session.state is not SessionState.IDLE:
            # Silently ignore — the add button will be disabled anyway,
            # and the visual affordance in the header tells the user to
            # stop the session first.
            return
        if self._discovery_modal is not None:
            self._discovery_modal.open()

    @staticmethod
    def _safe_call(fn) -> None:
        try:
            fn()
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Viewer session control call failed"
            )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _set_enabled(tag: str, enabled: bool) -> None:
    try:
        if enabled:
            dpg.enable_item(tag)
        else:
            dpg.disable_item(tag)
    except Exception:
        pass


def _format_time_short(at_ns: int) -> str:
    """Format a monotonic_ns timestamp as ``MM:SS.mmm`` for the table."""
    s = at_ns / 1e9
    minutes = int(s // 60)
    remainder = s - minutes * 60
    return f"{minutes:02d}:{remainder:06.3f}"


def _health_kind_color(kind: str):
    return {
        "heartbeat": theme.TEXT_MUTED,
        "drop": theme.WARNING,
        "reconnect": theme.INFO,
        "warning": theme.WARNING,
        "error": theme.DANGER,
    }.get(kind, theme.TEXT_SECONDARY)
