"""Desktop viewer modal for ``syncfield.discovery``.

When the user clicks "Discover devices" in the viewer header, this
modal opens, runs :func:`syncfield.discovery.scan` on a worker thread,
and presents the results as an OpenGraph-styled card list. The user
checks the devices they want, clicks "Add", and the selected devices
are constructed and registered with the live session.

Layout
------
::

    ┌── Discover devices ────────────────────────────┐
    │  Scan ready · last result 4.2 s ago            │
    │                                                 │
    │  Cameras                                        │
    │  ─────────                                      │
    │  ☑ FaceTime HD Camera       uvc_webcam · idx 0 │
    │  ☑ OAK-D S2                 oak_camera · 14…    │
    │                                                 │
    │  Sensors                                        │
    │  ───────                                        │
    │  ☑ OGLO Right               oglo_tactile · AA…  │
    │  ⚠ BNO085 Dongle            requires uuid       │
    │                                                 │
    │  [ Rescan ]                    [ Add 3 → ]     │
    └─────────────────────────────────────────────────┘

Threading
---------
All DearPyGui mutation runs on the main thread (via the viewer's
render loop, which calls ``update()`` every frame). The scan itself
runs in a daemon worker thread; when it completes, the worker updates
a small in-modal state object, and the next render-loop tick notices
the change and rebuilds the card list.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

import dearpygui.dearpygui as dpg

from syncfield.viewer import theme

if TYPE_CHECKING:
    from syncfield.discovery import DiscoveredDevice, DiscoveryReport
    from syncfield.orchestrator import SessionOrchestrator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State shared between the worker thread and the render loop
# ---------------------------------------------------------------------------


@dataclass
class _ModalState:
    """Mutable state the worker thread writes and the render loop reads.

    One instance per modal. The render loop polls ``needs_rebuild`` on
    every tick — cheap boolean check — and rebuilds the card list only
    when a scan has just completed or the selection set changed. This
    keeps the per-frame cost of having the modal open effectively zero.
    """

    scanning: bool = False
    scan_started_at: float = 0.0
    scan_completed_at: float = 0.0
    report: Optional["DiscoveryReport"] = None
    selected: set = field(default_factory=set)     # set[device_id]
    needs_rebuild: bool = False
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# Modal
# ---------------------------------------------------------------------------


class DiscoveryModal:
    """Modal window bound to a single :class:`SessionOrchestrator`.

    Constructed once by :class:`~syncfield.viewer.widgets.layout.ViewerLayout`
    and reused across opens. All DPG tags are namespaced under
    ``"discovery::"`` so the modal never collides with layout widgets.
    """

    _MODAL_WIDTH = 640
    _MODAL_HEIGHT = 620
    _SECTION_SPACING = 16

    def __init__(
        self,
        session: "SessionOrchestrator",
        *,
        on_added: Optional[Callable[[List["DiscoveredDevice"]], None]] = None,
    ) -> None:
        self._session = session
        self._on_added = on_added
        self._state = _ModalState()
        self._lock = threading.Lock()

        # DPG tags — constant strings so ``configure_item`` / ``set_value``
        # calls don't need to look anything up.
        self._window_tag = "discovery::window"
        self._status_tag = "discovery::status"
        self._content_tag = "discovery::content"
        self._rescan_button_tag = "discovery::btn_rescan"
        self._add_button_tag = "discovery::btn_add"
        self._close_button_tag = "discovery::btn_close"

        self._built = False

    # ------------------------------------------------------------------
    # Build / open / close
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Create the modal window. Idempotent — safe to call twice."""
        if self._built:
            return

        with dpg.window(
            label="Discover devices",
            tag=self._window_tag,
            width=self._MODAL_WIDTH,
            height=self._MODAL_HEIGHT,
            modal=True,
            show=False,
            no_resize=False,
            no_collapse=True,
            on_close=self._on_window_close,
        ):
            # Intro text sits at the very top and explains what's about
            # to happen in a single sentence.
            dpg.add_text(
                "Select cameras and sensors to register with this session.",
                color=theme.TEXT_SECONDARY,
            )
            dpg.add_spacer(height=8)

            # Status strip — shows "Ready", "Scanning…", or "Found N".
            with dpg.group(horizontal=True):
                dpg.add_text("●", tag="discovery::status_dot", color=theme.TEXT_MUTED)
                dpg.add_spacer(width=6)
                dpg.add_text(
                    "Ready",
                    tag=self._status_tag,
                    color=theme.TEXT_SECONDARY,
                )

            dpg.add_spacer(height=self._SECTION_SPACING)

            # Scrollable body where we draw device cards after a scan.
            dpg.add_child_window(
                tag=self._content_tag,
                width=-1,
                height=-60,  # leave room for footer buttons
                border=False,
                horizontal_scrollbar=False,
            )

            # Footer row: Rescan on the left, Add and Close on the right.
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Rescan",
                    tag=self._rescan_button_tag,
                    width=110,
                    height=32,
                    callback=self._on_rescan_click,
                )
                # Spacer pushes the next two buttons to the far right edge.
                dpg.add_spacer(width=self._MODAL_WIDTH - 110 - 200 - 80)
                dpg.add_button(
                    label="Close",
                    tag=self._close_button_tag,
                    width=90,
                    height=32,
                    callback=self._on_close_click,
                )
                dpg.add_button(
                    label="Add selected",
                    tag=self._add_button_tag,
                    width=140,
                    height=32,
                    callback=self._on_add_click,
                )

        # Bind button themes after the context has the tags in place.
        dpg.bind_item_theme(self._rescan_button_tag, theme.build_ghost_button_theme())
        dpg.bind_item_theme(self._close_button_tag, theme.build_ghost_button_theme())
        dpg.bind_item_theme(self._add_button_tag, theme.build_primary_button_theme())

        # Add button starts disabled — no selection until a scan finishes.
        dpg.disable_item(self._add_button_tag)

        self._built = True

    def open(self) -> None:
        """Show the modal and kick off a fresh scan on a worker thread."""
        if not self._built:
            self.build()
        dpg.show_item(self._window_tag)
        self._start_scan()

    def is_open(self) -> bool:
        return self._built and dpg.is_item_shown(self._window_tag)

    # ------------------------------------------------------------------
    # Render-loop integration
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """Called every render frame by :class:`ViewerLayout`.

        Checks the mutable state set by the worker thread and rebuilds
        the content area when a scan has just finished. No-op when the
        modal is closed or the scan hasn't produced new results.
        """
        if not self._built:
            return
        with self._lock:
            needs_rebuild = self._state.needs_rebuild
            if needs_rebuild:
                self._state.needs_rebuild = False

        if not needs_rebuild:
            # Still update the elapsed timer while scanning so the user
            # sees the progress tick forward.
            if self._state.scanning:
                elapsed = time.monotonic() - self._state.scan_started_at
                dpg.set_value(
                    self._status_tag, f"Scanning devices… {elapsed:.1f}s"
                )
            return

        # Snapshot the shared state under the lock, then render.
        with self._lock:
            report = self._state.report
            scanning = self._state.scanning
            error = self._state.error_message

        if scanning:
            # Worker said it's scanning but needs_rebuild was also set —
            # race where we clear content before showing the spinner.
            self._render_scanning_state()
        elif error:
            self._render_error_state(error)
        elif report is not None:
            self._render_results(report)

    # ------------------------------------------------------------------
    # Scan driving
    # ------------------------------------------------------------------

    def _start_scan(self) -> None:
        """Kick the background scan thread. Disables UI while it runs."""
        # Disable buttons so users can't double-click Rescan or Add while
        # the worker is mid-flight.
        dpg.disable_item(self._add_button_tag)
        dpg.disable_item(self._rescan_button_tag)
        dpg.configure_item("discovery::status_dot", color=theme.ACCENT)
        dpg.set_value(self._status_tag, "Scanning devices…")

        with self._lock:
            self._state.scanning = True
            self._state.scan_started_at = time.monotonic()
            self._state.report = None
            self._state.error_message = None
            self._state.selected.clear()
            self._state.needs_rebuild = True

        threading.Thread(
            target=self._run_scan_worker,
            name="discovery-modal-scan",
            daemon=True,
        ).start()

    def _run_scan_worker(self) -> None:
        """Background thread: call ``scan()`` and update shared state."""
        # Lazy import so importing the viewer package doesn't force the
        # discovery module load chain.
        from syncfield.discovery import scan

        try:
            report = scan(timeout=10.0, use_cache=False)
            error = None
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("discovery scan failed")
            report = None
            error = f"{type(exc).__name__}: {exc}"

        with self._lock:
            self._state.scanning = False
            self._state.scan_completed_at = time.monotonic()
            self._state.report = report
            self._state.error_message = error
            # Preselect every device that's ready to add (no warnings,
            # not in use) so the common "everything looks good, just
            # click Add" path is one click away.
            if report is not None:
                self._state.selected = {
                    d.device_id
                    for d in report.devices
                    if not d.warnings and not d.in_use
                }
            self._state.needs_rebuild = True

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _clear_content(self) -> None:
        """Wipe the content area before redrawing."""
        for child in dpg.get_item_children(self._content_tag, 1) or []:
            dpg.delete_item(child)

    def _render_scanning_state(self) -> None:
        self._clear_content()
        dpg.add_text(
            "Enumerating cameras and sensors…",
            parent=self._content_tag,
            color=theme.TEXT_SECONDARY,
        )
        dpg.add_text(
            "BLE peripherals take up to 5 seconds.",
            parent=self._content_tag,
            color=theme.TEXT_MUTED,
        )

    def _render_error_state(self, message: str) -> None:
        self._clear_content()
        dpg.configure_item("discovery::status_dot", color=theme.DANGER)
        dpg.set_value(self._status_tag, "Scan failed")
        dpg.add_text(
            "Discovery scan failed:",
            parent=self._content_tag,
            color=theme.DANGER,
        )
        dpg.add_text(message, parent=self._content_tag, color=theme.TEXT_SECONDARY)
        dpg.enable_item(self._rescan_button_tag)

    def _render_results(self, report: "DiscoveryReport") -> None:
        self._clear_content()

        count = len(report.devices)
        if count == 0:
            dpg.configure_item("discovery::status_dot", color=theme.TEXT_MUTED)
            dpg.set_value(
                self._status_tag,
                f"No devices found ({report.duration_s:.1f}s scan)",
            )
            dpg.add_text(
                "No cameras or sensors detected.",
                parent=self._content_tag,
                color=theme.TEXT_SECONDARY,
            )
            dpg.add_text(
                "Check cables, permissions, and make sure the SyncField "
                "extras ([uvc], [oak], [ble]) are installed.",
                parent=self._content_tag,
                color=theme.TEXT_MUTED,
                wrap=self._MODAL_WIDTH - 80,
            )
            dpg.enable_item(self._rescan_button_tag)
            return

        dpg.configure_item("discovery::status_dot", color=theme.SUCCESS)
        dpg.set_value(
            self._status_tag,
            f"Found {count} device{'s' if count != 1 else ''} in {report.duration_s:.1f}s",
        )

        # Group by Stream kind — cameras first, sensors second, others
        # last — so the eye reaches the most-relevant section first.
        for kind_key, title in (("video", "Cameras"), ("sensor", "Sensors"), ("audio", "Audio"), ("custom", "Other")):
            devices = report.by_kind(kind_key)
            if not devices:
                continue
            dpg.add_text(
                title.upper(),
                parent=self._content_tag,
                color=theme.TEXT_MUTED,
            )
            dpg.add_spacer(height=4, parent=self._content_tag)
            for device in devices:
                self._render_device_row(device)
            dpg.add_spacer(height=self._SECTION_SPACING, parent=self._content_tag)

        # Surface any scan errors at the bottom, muted.
        if report.errors:
            dpg.add_separator(parent=self._content_tag)
            dpg.add_text(
                "Scan errors (partial):",
                parent=self._content_tag,
                color=theme.TEXT_MUTED,
            )
            for adapter_type, error in report.errors.items():
                dpg.add_text(
                    f"· {adapter_type}: {error}",
                    parent=self._content_tag,
                    color=theme.WARNING,
                    wrap=self._MODAL_WIDTH - 80,
                )

        dpg.enable_item(self._rescan_button_tag)
        self._refresh_add_button_label()

    def _render_device_row(self, device: "DiscoveredDevice") -> None:
        """One row per discovered device — checkbox + two-line label."""
        addable = not device.warnings and not device.in_use
        checkbox_tag = f"discovery::check_{device.device_id}"
        row_tag = f"discovery::row_{device.device_id}"

        with dpg.group(tag=row_tag, parent=self._content_tag):
            with dpg.group(horizontal=True):
                dpg.add_checkbox(
                    tag=checkbox_tag,
                    default_value=device.device_id in self._state.selected,
                    callback=self._on_checkbox_toggle,
                    user_data=device.device_id,
                    enabled=addable,
                )
                dpg.add_spacer(width=4)
                dpg.add_text(
                    device.display_name,
                    color=theme.TEXT_PRIMARY if addable else theme.TEXT_MUTED,
                )

            # Sub-line: adapter_type · device_id · description
            sub_bits = [device.adapter_type]
            if device.device_id and device.device_id != device.display_name:
                sub_bits.append(device.device_id)
            if device.description:
                sub_bits.append(device.description)
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=24)  # align under the label
                dpg.add_text("  ·  ".join(sub_bits), color=theme.TEXT_MUTED)

            # Warning row if the device can't be auto-added.
            if device.warnings:
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=24)
                    dpg.add_text(f"⚠  {device.warnings[0]}", color=theme.WARNING)

            if device.in_use:
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=24)
                    dpg.add_text(
                        "⚠  already in use by another process",
                        color=theme.WARNING,
                    )

            dpg.add_spacer(height=6)

    def _refresh_add_button_label(self) -> None:
        """Keep the Add button label in sync with the selection size."""
        n = len(self._state.selected)
        if n == 0:
            dpg.set_item_label(self._add_button_tag, "Add selected")
            dpg.disable_item(self._add_button_tag)
        else:
            dpg.set_item_label(
                self._add_button_tag,
                f"Add {n} device{'s' if n != 1 else ''}",
            )
            dpg.enable_item(self._add_button_tag)

    # ------------------------------------------------------------------
    # Button callbacks
    # ------------------------------------------------------------------

    def _on_checkbox_toggle(self, sender: Any, value: bool, user_data: Any) -> None:
        device_id = str(user_data)
        with self._lock:
            if value:
                self._state.selected.add(device_id)
            else:
                self._state.selected.discard(device_id)
        self._refresh_add_button_label()

    def _on_rescan_click(self) -> None:
        self._start_scan()

    def _on_add_click(self) -> None:
        """Construct and register each selected device with the session.

        Runs on the UI thread (it's a button callback) but the
        ``session.add()`` and stream construction calls are fast — no
        real I/O, no BLE connect — so blocking briefly is fine.
        """
        from syncfield.discovery import make_stream_id

        with self._lock:
            report = self._state.report
            selected = set(self._state.selected)

        if not report or not selected:
            return

        existing_ids = set(self._session._streams.keys())  # noqa: SLF001
        added: List["DiscoveredDevice"] = []

        for device in report.devices:
            if device.device_id not in selected:
                continue
            try:
                stream_id = make_stream_id(device.display_name, existing_ids)
                kwargs: Dict[str, Any] = {"id": stream_id}
                if device.accepts_output_dir:
                    kwargs["output_dir"] = self._session.output_dir
                stream = device.construct(**kwargs)
                self._session.add(stream)
                existing_ids.add(stream_id)
                added.append(device)
            except Exception as exc:
                logger.warning(
                    "failed to add %s: %s: %s",
                    device.display_name,
                    type(exc).__name__,
                    exc,
                )

        if added and self._on_added is not None:
            try:
                self._on_added(added)
            except Exception:
                logger.exception("on_added callback raised")

        dpg.hide_item(self._window_tag)

    def _on_close_click(self) -> None:
        dpg.hide_item(self._window_tag)

    def _on_window_close(self, sender: Any) -> None:
        """Called when the user clicks the native ``X`` on the modal."""
        # Nothing to clean up — the scan thread is daemonized and the
        # DPG state is reused on the next open().
        pass
