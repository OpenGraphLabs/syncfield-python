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
from syncfield.viewer.fonts import FontRegistry
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

    #: How long the countdown runs before recording actually begins.
    #: The session clock panel overlays ``3 → 2 → 1`` in big display
    #: numerals during this window.
    COUNTDOWN_SECONDS: int = 3

    def __init__(
        self,
        session: SessionOrchestrator,
        *,
        fonts: Optional[FontRegistry] = None,
    ) -> None:
        self._session = session
        self._fonts = fonts or FontRegistry()
        self._cards: Dict[str, StreamCard] = {}
        self._streams_row_tag = "streams_row"
        self._health_table_tag = "health_table"
        self._last_health_keys: tuple = ()
        # Discovery modal — built lazily the first time the user clicks
        # the header button. Holds its own DPG tags so the layout does
        # not need to know about its internals.
        self._discovery_modal: Optional[DiscoveryModal] = None
        # Countdown state — populated by the Record callback, read by
        # ``_update_clock_panel`` so the big overlay number stays in
        # sync with ``SessionOrchestrator.start(on_countdown_tick=…)``.
        self._countdown_value: Optional[int] = None
        self._countdown_lock = threading.Lock()

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
            no_scrollbar=True,
        ):
            self._build_header()
            dpg.add_spacer(height=2)
            dpg.add_separator()
            dpg.add_spacer(height=10)
            self._build_control_and_clock_row()
            dpg.add_spacer(height=14)
            self._build_streams_section()
            dpg.add_spacer(height=14)
            self._build_health_section()
            dpg.add_spacer(height=10)
            dpg.add_separator()
            dpg.add_spacer(height=8)
            self._build_footer()

        # Button themes need the context to exist, so build them now.
        self._primary_theme = theme.build_primary_button_theme()
        self._danger_theme = theme.build_danger_button_theme()
        self._ghost_theme = theme.build_ghost_button_theme()
        self._soft_panel_theme = theme.build_soft_panel_theme()

        dpg.bind_item_theme("control_panel", self._soft_panel_theme)
        dpg.bind_item_theme("clock_panel", self._soft_panel_theme)
        dpg.bind_item_theme("btn_connect", self._primary_theme)
        dpg.bind_item_theme("btn_record", self._primary_theme)
        dpg.bind_item_theme("btn_stop", self._danger_theme)
        dpg.bind_item_theme("btn_cancel", self._ghost_theme)
        dpg.bind_item_theme("btn_discover", self._ghost_theme)

        # Typography — bind prominent display fonts to the app title,
        # timer, and host id so the header feels like a real app.
        self._bind_fonts()

        # Construct (but don't yet show) the discovery modal. Building
        # it here means the first click on the Discover button opens
        # an already-ready window instead of waiting for DPG to build
        # on demand.
        self._discovery_modal = DiscoveryModal(self._session, fonts=self._fonts)
        self._discovery_modal.build()

        # The viewer intentionally does NOT auto-connect. The user
        # clicks the Connect button when they're ready, at which
        # point the session transitions IDLE → CONNECTED and every
        # adapter starts producing live preview frames for its card.
        # Before that click the viewer sits in IDLE with empty
        # stream cards, which gives the user a beat to review the
        # registered streams and run Discovery if needed.

    def _bind_fonts(self) -> None:
        """Assign per-widget fonts from the shared :class:`FontRegistry`.

        Safe to call with an empty registry — missing font tags become
        no-ops, and the widget keeps whatever the global default font is.
        """
        def bind(tag: str, font_tag: Optional[int]) -> None:
            if font_tag is not None:
                try:
                    dpg.bind_item_font(tag, font_tag)
                except Exception:
                    pass

        # App title — display size
        bind("app_title", self._fonts.ui_lg)
        # Host id — monospace so varying-width ids don't jitter the header
        bind("host_id_text", self._fonts.mono)
        # State label — slightly larger than body for chip-like emphasis
        bind("state_label", self._fonts.ui_md)
        # Elapsed timer — monospace so digits don't shift sub-pixel
        bind("elapsed_text", self._fonts.mono)
        # Section titles
        for tag in (
            "label_controls",
            "label_clock",
            "label_streams",
            "label_health",
            "label_output",
            "label_wall_clock",
        ):
            bind(tag, self._fonts.ui_sm)
        # Monospace clock values
        bind("sync_point_text", self._fonts.mono)
        bind("chirp_text", self._fonts.mono)
        bind("wall_clock_text", self._fonts.mono)
        bind("output_text", self._fonts.mono)
        bind("tagline_text", self._fonts.ui_sm)
        # Big countdown overlay — use the largest display font
        bind("countdown_overlay", self._fonts.ui_lg)

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------

    def _build_header(self) -> None:
        """Top row: logo, host id, state chip, elapsed timer, discover button.

        Laid out as one horizontal group with a spring spacer that pushes
        the Discover button to the far edge. Right-alignment is
        approximate — DearPyGui doesn't have a real flexbox spacer, so we
        compute the push width from :data:`theme.VIEWPORT_WIDTH`. The
        primary window is pinned to the viewport so resize-driven drift
        is acceptable for v1.
        """
        with dpg.group(horizontal=True):
            dpg.add_text("SyncField", tag="app_title")
            dpg.add_spacer(width=18)
            dpg.add_text(
                self._session.host_id,
                tag="host_id_text",
                color=theme.TEXT_SECONDARY,
            )
            dpg.add_spacer(width=24)
            dpg.add_text("●", tag="state_dot", color=theme.STATE_IDLE)
            dpg.add_spacer(width=6)
            dpg.add_text(
                "IDLE",
                tag="state_label",
                color=theme.TEXT_PRIMARY,
            )
            dpg.add_spacer(width=18)
            dpg.add_text(
                "00:00.000",
                tag="elapsed_text",
                color=theme.TEXT_SECONDARY,
            )
            # Spring spacer — sized to leave room for the button at the
            # right edge of the window's content area.
            dpg.add_spacer(width=_header_spring_width())
            dpg.add_button(
                label="Discover devices",
                tag="btn_discover",
                width=180,
                height=32,
                callback=self._on_discover_click,
            )

        dpg.add_spacer(height=6)
        dpg.add_text(
            "Capture orchestration  ·  live session view",
            tag="tagline_text",
            color=theme.TEXT_MUTED,
        )

    def _build_control_and_clock_row(self) -> None:
        """Two side-by-side panels: controls + session clock.

        Control panel button stack (top to bottom):

            [         Connect         ]   ← IDLE only; opens devices + live preview
            [ Record  ] [    Stop    ]    ← Record is CONNECTED-only; Stop is RECORDING-only
            [          Cancel          ]   ← COUNTDOWN / RECORDING only

        Connect is a separate explicit step so the user can open the
        viewer, review the registered streams, run Discovery if they
        want, and then kick off device I/O with a single click —
        nothing opens a camera handle until they ask for it.
        """
        with dpg.group(horizontal=True):
            # --- Control panel ----------------------------------------
            with dpg.child_window(
                tag="control_panel",
                width=260,
                height=theme.CONTROL_PANEL_HEIGHT,
                border=False,
                no_scrollbar=True,
            ):
                dpg.add_text(
                    "CONTROLS", tag="label_controls", color=theme.TEXT_MUTED,
                )
                dpg.add_spacer(height=10)
                # Row 1 — Connect (primary in IDLE, disabled otherwise)
                dpg.add_button(
                    label="Connect",
                    tag="btn_connect",
                    width=212,
                    height=34,
                    callback=self._on_connect_click,
                )
                dpg.add_spacer(height=8)
                # Row 2 — Record + Stop (split 112 / 92 = 204 + 8 gap)
                with dpg.group(horizontal=True):
                    dpg.add_button(
                        label="Record",
                        tag="btn_record",
                        width=112,
                        height=34,
                        callback=self._on_record_click,
                    )
                    dpg.add_button(
                        label="Stop",
                        tag="btn_stop",
                        width=92,
                        height=34,
                        callback=self._on_stop_click,
                    )
                dpg.add_spacer(height=8)
                # Row 3 — Cancel (aborts COUNTDOWN / best-effort Stop)
                dpg.add_button(
                    label="Cancel",
                    tag="btn_cancel",
                    width=212,
                    height=28,
                    callback=self._on_cancel_click,
                )

            dpg.add_spacer(width=14)

            # --- Session clock + chirp panel --------------------------
            with dpg.child_window(
                tag="clock_panel",
                width=-1,
                height=theme.CONTROL_PANEL_HEIGHT,
                border=False,
                no_scrollbar=True,
            ):
                # Header row — section label on the left, big countdown
                # number on the right. The countdown is hidden by
                # default and ``_update_clock_panel`` toggles it
                # visible whenever the orchestrator is in the
                # COUNTDOWN state.
                with dpg.group(horizontal=True):
                    dpg.add_text(
                        "SESSION CLOCK",
                        tag="label_clock",
                        color=theme.TEXT_MUTED,
                    )
                    dpg.add_spacer(width=12)
                    dpg.add_text(
                        "",
                        tag="countdown_overlay",
                        color=theme.ACCENT,
                        show=False,
                    )
                dpg.add_spacer(height=12)

                # Key / value strip — one row per field, fixed-width
                # label column so values line up. A plain horizontal
                # group with a single inline spacer avoids the extra
                # vertical padding a nested fixed-width group would add.
                def _kv_row(label_text: str, value_tag: str, default: str) -> None:
                    with dpg.group(horizontal=True):
                        dpg.add_text(label_text, color=theme.TEXT_SECONDARY)
                        # Trailing spacer width is computed from the
                        # longest label ("sync_point") so every value
                        # column aligns on the same x coordinate.
                        pad = _kv_label_pad(label_text)
                        if pad > 0:
                            dpg.add_spacer(width=pad)
                        dpg.add_text(default, tag=value_tag)

                _kv_row("sync_point", "sync_point_text", "—")
                _kv_row("chirp", "chirp_text", "pending")
                _kv_row("tone", "tone_text", "—")

    def _build_streams_section(self) -> None:
        """Horizontal scrollable row of stream cards."""
        dpg.add_text(
            "STREAMS", tag="label_streams", color=theme.TEXT_MUTED,
        )
        dpg.add_spacer(height=8)
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
        dpg.add_text(
            "HEALTH EVENTS", tag="label_health", color=theme.TEXT_MUTED,
        )
        dpg.add_spacer(height=8)
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
        """Output path and wall clock, as a two-column key/value strip."""
        with dpg.group(horizontal=True):
            dpg.add_text("output", tag="label_output", color=theme.TEXT_MUTED)
            dpg.add_spacer(width=_kv_label_pad("output"))
            dpg.add_text("—", tag="output_text", color=theme.TEXT_SECONDARY)
        with dpg.group(horizontal=True):
            dpg.add_text("wall clock", tag="label_wall_clock", color=theme.TEXT_MUTED)
            dpg.add_spacer(width=_kv_label_pad("wall clock"))
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

        # Countdown overlay — visible only while the orchestrator is
        # in the COUNTDOWN state. The big number is pulled from the
        # worker-thread-populated ``_countdown_value`` under a lock.
        if snapshot.state == "countdown":
            with self._countdown_lock:
                value = self._countdown_value
            if value is not None:
                dpg.set_value("countdown_overlay", f"· {value} ·")
                dpg.configure_item("countdown_overlay", show=True)
        else:
            dpg.configure_item("countdown_overlay", show=False)

    def _update_controls(self, snapshot: SessionSnapshot) -> None:
        """Enable/disable the session control buttons based on state.

        The 0.2 lifecycle has more states than the 0.1 one:

        * ``IDLE`` / ``CONNECTING`` — nothing is enabled. The viewer
          is about to transition into CONNECTED via the auto-connect
          worker thread; buttons stay greyed-out until it lands.
        * ``CONNECTED`` — Record is primary-enabled. Discovery is
          also allowed since no recording is in flight.
        * ``COUNTDOWN`` / ``PREPARING`` — all buttons disabled except
          Cancel (which aborts the countdown or the prepare phase).
        * ``RECORDING`` — Stop is the primary action; Cancel also
          triggers a stop (best-effort path).
        * ``STOPPING`` / ``STOPPED`` — everything disabled while the
          finalize path runs. After STOPPED, the auto-connect path
          teardown has completed and the viewer is typically closing.
        """
        state = snapshot.state
        # Connect: the one gateway out of IDLE. After the user clicks
        # it the session walks IDLE → CONNECTING → CONNECTED on its
        # own, so the button stays disabled from CONNECTING onward.
        _set_enabled("btn_connect", state == "idle")
        _set_enabled("btn_record", state == "connected")
        _set_enabled("btn_stop", state == "recording")
        _set_enabled(
            "btn_cancel",
            state in ("preparing", "countdown", "recording"),
        )
        # Discovery is allowed before connecting (IDLE) so the user
        # can add more streams to the session before live preview
        # starts. ``scan_and_add`` requires IDLE anyway.
        _set_enabled("btn_discover", state == "idle")

    def _update_streams(self, snapshot: SessionSnapshot, now_ns: int) -> None:
        # Create cards for new streams.
        for stream_id, stream_snap in snapshot.streams.items():
            if stream_id not in self._cards:
                self._cards[stream_id] = StreamCard(
                    self._streams_row_tag,
                    stream_snap,
                    fonts=self._fonts,
                    on_remove=self._request_remove_stream,
                )
            self._cards[stream_id].update(
                stream_snap, now_ns, session_state=snapshot.state
            )

        # Cards for streams that were removed (either by the × button on
        # the card itself, via code, or by a rollback). Pop the card from
        # our dict and delete its DPG node so the row reflows.
        removed = set(self._cards.keys()) - set(snapshot.streams.keys())
        for stream_id in removed:
            card = self._cards.pop(stream_id)
            try:
                dpg.delete_item(card._card_tag)  # noqa: SLF001
            except Exception:
                pass

    def _request_remove_stream(self, stream_id: str) -> None:
        """Drive ``SessionOrchestrator.remove`` on a worker thread.

        Called by a stream card's × button. The orchestrator's
        ``remove()`` may call ``stream.disconnect()`` on a live
        hardware handle, which can take tens of ms, so we never run it
        on the DPG render thread. On success the next poller tick
        notices that ``stream_id`` dropped out of the session and the
        render loop deletes the DPG card node in
        :meth:`_update_streams`.

        Failures are swallowed via :meth:`_safe_call` (logged at
        ERROR) so a transient remove error never freezes the UI.
        """
        threading.Thread(
            target=self._safe_call,
            args=(lambda: self._session.remove(stream_id),),
            name=f"viewer-remove-{stream_id}",
            daemon=True,
        ).start()

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

    def _on_connect_click(self) -> None:
        """Open devices on every registered stream (IDLE → CONNECTED).

        Dispatched onto a worker thread because device open is slow
        for real hardware (several hundred ms per UVC device, longer
        for BLE). After ``connect()`` returns the session is in
        ``CONNECTED`` and each adapter's capture loop is publishing
        live frames to its :attr:`latest_frame` / plot buffers — the
        next render tick picks them up and the stream cards go live.

        Errors from ``connect()`` are logged by :meth:`_safe_call`
        and leave the session back in ``IDLE`` so the user can
        retry without closing the viewer.
        """
        threading.Thread(
            target=self._safe_call,
            args=(self._session.connect,),
            name="viewer-ctrl-connect",
            daemon=True,
        ).start()

    def _on_record_click(self) -> None:
        """Trigger the full start flow: countdown → record → chirp.

        Dispatched onto a worker thread so the render loop stays
        responsive while the countdown sleeps and the streams begin
        writing. The orchestrator fires ``on_countdown_tick`` once
        per remaining second; the callback stores the value on the
        shared lock so the next render frame's ``_update_clock_panel``
        shows the big overlay.
        """
        def _run_start() -> None:
            try:
                self._session.start(
                    countdown_s=self.COUNTDOWN_SECONDS,
                    on_countdown_tick=self._on_countdown_tick,
                )
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "Viewer session.start() failed"
                )
            finally:
                with self._countdown_lock:
                    self._countdown_value = None

        threading.Thread(
            target=_run_start,
            name="viewer-ctrl-start",
            daemon=True,
        ).start()

    def _on_countdown_tick(self, n: int) -> None:
        """Called from the orchestrator's start worker, per tick.

        Runs on the worker thread, not the render thread — we just
        store the value under a lock and let the next frame's
        ``_update_clock_panel`` call render it.
        """
        with self._countdown_lock:
            self._countdown_value = n

    def _on_stop_click(self) -> None:
        threading.Thread(
            target=self._safe_call,
            args=(self._session.stop,),
            name="viewer-ctrl-stop",
            daemon=True,
        ).start()

    def _on_cancel_click(self) -> None:
        """Cancel during COUNTDOWN or RECORDING.

        The SDK has no dedicated cancel primitive; calling
        :meth:`SessionOrchestrator.stop` takes the best-effort path
        for both cases. Cancelling during the countdown is
        interpreted as "don't record this one" — because no stream
        has received ``start_recording`` yet, the stop path is a
        no-op on the streams and the chirp is skipped.
        """
        state = self._session.state
        if state is SessionState.RECORDING:
            self._on_stop_click()
        elif state is SessionState.COUNTDOWN:
            # We can't interrupt the countdown sleep from here, but
            # we can mark the user intent so when the countdown
            # finishes the subsequent stop picks it up. For v1 this
            # is a soft cancel: the recording starts briefly and
            # then immediately stops.
            def _cancel_after_start() -> None:
                # Wait for the session to leave COUNTDOWN
                import time as _t

                deadline = _t.monotonic() + 5.0
                while _t.monotonic() < deadline:
                    if self._session.state is SessionState.RECORDING:
                        try:
                            self._session.stop()
                        except Exception:
                            pass
                        return
                    _t.sleep(0.05)

            threading.Thread(
                target=_cancel_after_start,
                name="viewer-ctrl-cancel",
                daemon=True,
            ).start()

    def _on_discover_click(self) -> None:
        """Open the discovery modal.

        Allowed from ``IDLE`` / ``CONNECTED`` / ``STOPPED``. Silently
        ignored while a recording is in flight — ``scan_and_add``
        refuses anyway, and the button disables itself in those
        states.
        """
        if self._session.state not in (
            SessionState.IDLE,
            SessionState.CONNECTED,
            SessionState.STOPPED,
        ):
            return
        if self._discovery_modal is not None:
            self._discovery_modal.open()

    # ------------------------------------------------------------------
    # Session lifecycle teardown — called by the viewer app on close
    # ------------------------------------------------------------------

    def teardown_session(self) -> None:
        """Return the session to ``IDLE`` when the viewer is closing.

        Called from :class:`ViewerApp.close`. Handles all the
        intermediate states the session might be in when the user
        closes the window mid-recording:

        * ``RECORDING`` → stop() then disconnect()
        * ``CONNECTED`` / ``STOPPED`` → disconnect()
        * everything else → best-effort, swallow errors

        Runs on the caller's thread (the viewer shutdown path) so the
        orchestrator's lifecycle lock is respected.
        """
        try:
            if self._session.state is SessionState.RECORDING:
                self._session.stop()
            if self._session.state in (SessionState.CONNECTED, SessionState.STOPPED):
                self._session.disconnect()
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Viewer session teardown failed"
            )

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


def _kv_label_pad(label: str) -> int:
    """Return the pixel spacer width that aligns a value column for ``label``.

    The viewer's key/value strips (session clock, footer) use the longest
    label in the column as the alignment anchor. This helper hard-codes
    the pixel widths because DearPyGui doesn't expose a text-metrics API
    before the viewport is shown — at build time the font has been
    loaded but the renderer's atlas is not yet available.

    The numbers were measured at 15 px SF Pro (the viewer's default body
    font) against a 90 px value column anchor. Fonts at other sizes drift
    a few pixels but the layout reads correctly down to 13 px.
    """
    # Keyed by label — one entry per text we render in a key/value row.
    # Anchor column x = 100 px from the start of the row.
    _ANCHOR_X = 100
    _LABEL_W = {
        "sync_point": 76,
        "chirp": 38,
        "tone": 33,
        "output": 48,
        "wall clock": 74,
    }
    width = _LABEL_W.get(label, 0)
    return max(8, _ANCHOR_X - width)


def _header_spring_width() -> int:
    """Return a spacer width that roughly right-aligns the Discover button.

    DearPyGui has no flexbox spring spacer, so we compute the gap from
    the fixed viewport width minus the estimated left-cluster width and
    the button's declared width. This is intentionally rough — the
    primary window is pinned to a 1200 px viewport in the default
    layout, so the approximation is good enough for the common case and
    the layout does not need to react to resize.
    """
    # Window content width = viewport width - left/right window padding.
    content_w = theme.VIEWPORT_WIDTH - 2 * theme.WINDOW_PADDING[0]
    # Rough pixel width of the left cluster (title + host + state + timer
    # + fixed spacers). Overestimates slightly so the button never gets
    # clipped when the timer grows to ``99:59.999``.
    left_cluster_w = 430
    # Discover button declared width.
    button_w = 180
    spring = content_w - left_cluster_w - button_w
    return max(40, spring)


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
