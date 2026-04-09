"""Visual theme for the SyncField desktop viewer.

The viewer ships a **light theme only**, matching OpenGraph's minimal and
sophisticated design language. Everything here is in one place so a future
dark mode (or brand recolor) is a single-file change.

Design tokens:

- Backgrounds and surfaces use a near-white palette with subtle tonal
  variation so panels pop without needing heavy borders.
- Primary text is near-black, secondary text is a muted gray.
- Accent color is a single calibrated indigo used for state indicators and
  active buttons. Success/warning/danger round out the semantic palette.
- Border radii are consistently soft (6–10 px) so the GUI feels modern
  without being cartoonish.
- Padding and spacing values are generous, matching OpenGraph's docs site
  aesthetic of "plenty of whitespace, tight typography."

All values are raw tuples so the module is importable without DearPyGui —
the ``build_theme`` function is the only thing that touches ``dpg``.
"""

from __future__ import annotations

from typing import Any, Tuple

# ---------------------------------------------------------------------------
# Color palette (RGBA 0-255)
# ---------------------------------------------------------------------------

# Surfaces — near-white tonal stack
BG_APP = (248, 249, 251, 255)          # #F8F9FB  — viewport background
BG_PANEL = (255, 255, 255, 255)        # #FFFFFF  — primary panels
BG_PANEL_SOFT = (243, 245, 248, 255)   # #F3F5F8  — secondary / nested panels
BG_HOVER = (237, 240, 244, 255)        # #EDF0F4
BG_ACTIVE = (228, 232, 239, 255)       # #E4E8EF

# Borders
BORDER_SUBTLE = (228, 231, 236, 255)   # #E4E7EC  — panel hairlines
BORDER_STRONG = (209, 213, 219, 255)   # #D1D5DB  — emphasized borders

# Text
TEXT_PRIMARY = (17, 24, 39, 255)       # #111827  — gray-900
TEXT_SECONDARY = (107, 114, 128, 255)  # #6B7280  — gray-500
TEXT_MUTED = (156, 163, 175, 255)      # #9CA3AF  — gray-400
TEXT_ON_ACCENT = (255, 255, 255, 255)

# Semantic colors
ACCENT = (79, 70, 229, 255)            # #4F46E5  — indigo-600 (primary brand)
ACCENT_HOVER = (67, 56, 202, 255)      # #4338CA
ACCENT_ACTIVE = (55, 48, 163, 255)     # #3730A3
ACCENT_SOFT = (238, 242, 255, 255)     # #EEF2FF  — indigo-50 tint

SUCCESS = (16, 185, 129, 255)          # #10B981  — emerald-500
SUCCESS_SOFT = (209, 250, 229, 255)    # #D1FAE5
WARNING = (245, 158, 11, 255)          # #F59E0B  — amber-500
WARNING_SOFT = (254, 243, 199, 255)    # #FEF3C7
DANGER = (220, 38, 38, 255)            # #DC2626  — red-600
DANGER_SOFT = (254, 226, 226, 255)     # #FEE2E2
INFO = (14, 165, 233, 255)             # #0EA5E9  — sky-500

# Session state indicators
STATE_IDLE = TEXT_MUTED
STATE_PREPARING = WARNING
STATE_RECORDING = DANGER
STATE_STOPPING = WARNING
STATE_STOPPED = SUCCESS

# Plot colors (palette matches Tailwind's calibrated hues for print/light bg)
PLOT_SERIES_COLORS: Tuple[Tuple[int, int, int, int], ...] = (
    (79, 70, 229, 255),    # indigo-600
    (16, 185, 129, 255),   # emerald-500
    (245, 158, 11, 255),   # amber-500
    (220, 38, 38, 255),    # red-600
    (14, 165, 233, 255),   # sky-500
    (236, 72, 153, 255),   # pink-500
    (20, 184, 166, 255),   # teal-500
)


# ---------------------------------------------------------------------------
# Spacing and typography scale
# ---------------------------------------------------------------------------

# Style variables (DearPyGui ImGui-style)
FRAME_ROUNDING = 6
WINDOW_ROUNDING = 10
CHILD_ROUNDING = 8
POPUP_ROUNDING = 8
GRAB_ROUNDING = 4
SCROLLBAR_ROUNDING = 6
TAB_ROUNDING = 6

FRAME_PADDING = (12, 8)
WINDOW_PADDING = (20, 20)
ITEM_SPACING = (12, 10)
ITEM_INNER_SPACING = (8, 6)
CELL_PADDING = (8, 6)

WINDOW_BORDER_SIZE = 0
CHILD_BORDER_SIZE = 1
FRAME_BORDER_SIZE = 1
POPUP_BORDER_SIZE = 1

# Card dimensions
CARD_WIDTH = 260
CARD_HEIGHT = 300
VIDEO_THUMBNAIL_HEIGHT = 146  # 16:9 at 260 width
PLOT_HEIGHT = 146

# Layout sections
HEADER_HEIGHT = 72
CONTROL_PANEL_HEIGHT = 160
STREAMS_SECTION_HEIGHT = 340
HEALTH_SECTION_HEIGHT = 180
FOOTER_HEIGHT = 48

VIEWPORT_WIDTH = 1200
VIEWPORT_HEIGHT = 900


# ---------------------------------------------------------------------------
# Theme builder — the only function that imports DearPyGui
# ---------------------------------------------------------------------------


def build_theme() -> int:
    """Construct the OpenGraph light theme and return its DPG tag.

    Call this after ``dpg.create_context()`` and bind with
    ``dpg.bind_theme(tag)``.
    """
    import dearpygui.dearpygui as dpg

    with dpg.theme() as theme_tag:
        with dpg.theme_component(dpg.mvAll):
            # --- Window + child backgrounds -----------------------------
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, BG_APP)
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, BG_PANEL)
            dpg.add_theme_color(dpg.mvThemeCol_PopupBg, BG_PANEL)
            dpg.add_theme_color(dpg.mvThemeCol_MenuBarBg, BG_PANEL)
            dpg.add_theme_color(dpg.mvThemeCol_Border, BORDER_SUBTLE)
            dpg.add_theme_color(dpg.mvThemeCol_BorderShadow, (0, 0, 0, 0))

            # --- Text ---------------------------------------------------
            dpg.add_theme_color(dpg.mvThemeCol_Text, TEXT_PRIMARY)
            dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, TEXT_MUTED)
            dpg.add_theme_color(dpg.mvThemeCol_TextSelectedBg, ACCENT_SOFT)

            # --- Titles -------------------------------------------------
            dpg.add_theme_color(dpg.mvThemeCol_TitleBg, BG_PANEL)
            dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, BG_PANEL)
            dpg.add_theme_color(dpg.mvThemeCol_TitleBgCollapsed, BG_PANEL_SOFT)

            # --- Frames (input boxes, sliders) -------------------------
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, BG_PANEL_SOFT)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, BG_HOVER)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, BG_ACTIVE)

            # --- Buttons ------------------------------------------------
            dpg.add_theme_color(dpg.mvThemeCol_Button, BG_PANEL_SOFT)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, BG_HOVER)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, BG_ACTIVE)

            # --- Headers (collapsing, tree, selectable) ----------------
            dpg.add_theme_color(dpg.mvThemeCol_Header, BG_PANEL_SOFT)
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, BG_HOVER)
            dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, BG_ACTIVE)

            # --- Separators ---------------------------------------------
            dpg.add_theme_color(dpg.mvThemeCol_Separator, BORDER_SUBTLE)
            dpg.add_theme_color(dpg.mvThemeCol_SeparatorHovered, BORDER_STRONG)
            dpg.add_theme_color(dpg.mvThemeCol_SeparatorActive, ACCENT)

            # --- Scrollbars ---------------------------------------------
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (0, 0, 0, 0))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, BORDER_STRONG)
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered, TEXT_MUTED)
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabActive, TEXT_SECONDARY)

            # --- Checkbox / radio / slider grabs -----------------------
            dpg.add_theme_color(dpg.mvThemeCol_CheckMark, ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, ACCENT_ACTIVE)

            # --- Tabs ---------------------------------------------------
            dpg.add_theme_color(dpg.mvThemeCol_Tab, BG_PANEL_SOFT)
            dpg.add_theme_color(dpg.mvThemeCol_TabHovered, BG_HOVER)
            dpg.add_theme_color(dpg.mvThemeCol_TabActive, BG_PANEL)
            dpg.add_theme_color(dpg.mvThemeCol_TabUnfocused, BG_PANEL_SOFT)
            dpg.add_theme_color(dpg.mvThemeCol_TabUnfocusedActive, BG_PANEL)

            # --- Tables -------------------------------------------------
            dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg, BG_PANEL_SOFT)
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong, BORDER_STRONG)
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, BORDER_SUBTLE)
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBg, BG_PANEL)
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt, BG_PANEL_SOFT)

            # --- Plot lines ---------------------------------------------
            dpg.add_theme_color(dpg.mvThemeCol_PlotLines, ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_PlotLinesHovered, ACCENT_HOVER)
            dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_PlotHistogramHovered, ACCENT_HOVER)

            # --- Style variables ----------------------------------------
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, WINDOW_ROUNDING)
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, CHILD_ROUNDING)
            dpg.add_theme_style(dpg.mvStyleVar_PopupRounding, POPUP_ROUNDING)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, FRAME_ROUNDING)
            dpg.add_theme_style(dpg.mvStyleVar_ScrollbarRounding, SCROLLBAR_ROUNDING)
            dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, GRAB_ROUNDING)
            dpg.add_theme_style(dpg.mvStyleVar_TabRounding, TAB_ROUNDING)

            dpg.add_theme_style(
                dpg.mvStyleVar_WindowPadding, WINDOW_PADDING[0], WINDOW_PADDING[1]
            )
            dpg.add_theme_style(
                dpg.mvStyleVar_FramePadding, FRAME_PADDING[0], FRAME_PADDING[1]
            )
            dpg.add_theme_style(
                dpg.mvStyleVar_ItemSpacing, ITEM_SPACING[0], ITEM_SPACING[1]
            )
            dpg.add_theme_style(
                dpg.mvStyleVar_ItemInnerSpacing,
                ITEM_INNER_SPACING[0],
                ITEM_INNER_SPACING[1],
            )
            dpg.add_theme_style(
                dpg.mvStyleVar_CellPadding, CELL_PADDING[0], CELL_PADDING[1]
            )

            dpg.add_theme_style(dpg.mvStyleVar_WindowBorderSize, WINDOW_BORDER_SIZE)
            dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, CHILD_BORDER_SIZE)
            dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, FRAME_BORDER_SIZE)
            dpg.add_theme_style(dpg.mvStyleVar_PopupBorderSize, POPUP_BORDER_SIZE)

    return theme_tag


# ---------------------------------------------------------------------------
# Button variants — one theme per semantic role
# ---------------------------------------------------------------------------


def build_primary_button_theme() -> int:
    """Filled indigo button theme for the primary action (Record)."""
    import dearpygui.dearpygui as dpg

    with dpg.theme() as theme_tag:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, ACCENT_HOVER)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, ACCENT_ACTIVE)
            dpg.add_theme_color(dpg.mvThemeCol_Text, TEXT_ON_ACCENT)
            dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 0)
    return theme_tag


def build_danger_button_theme() -> int:
    """Filled red button theme for stop/danger actions."""
    import dearpygui.dearpygui as dpg

    with dpg.theme() as theme_tag:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, DANGER)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (185, 28, 28, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (153, 27, 27, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Text, TEXT_ON_ACCENT)
            dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 0)
    return theme_tag


def build_ghost_button_theme() -> int:
    """Outlined/subtle button theme for secondary actions (Cancel)."""
    import dearpygui.dearpygui as dpg

    with dpg.theme() as theme_tag:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, BG_HOVER)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, BG_ACTIVE)
            dpg.add_theme_color(dpg.mvThemeCol_Text, TEXT_SECONDARY)
            dpg.add_theme_color(dpg.mvThemeCol_Border, BORDER_STRONG)
            dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1)
    return theme_tag


def build_card_theme() -> int:
    """Theme for stream cards — white panel with a subtle hairline border."""
    import dearpygui.dearpygui as dpg

    with dpg.theme() as theme_tag:
        with dpg.theme_component(dpg.mvChildWindow):
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, BG_PANEL)
            dpg.add_theme_color(dpg.mvThemeCol_Border, BORDER_SUBTLE)
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, CHILD_ROUNDING)
            dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, 1)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 16, 14)
    return theme_tag


def build_soft_panel_theme() -> int:
    """Theme for secondary panels — light gray fill, no border."""
    import dearpygui.dearpygui as dpg

    with dpg.theme() as theme_tag:
        with dpg.theme_component(dpg.mvChildWindow):
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, BG_PANEL_SOFT)
            dpg.add_theme_color(dpg.mvThemeCol_Border, (0, 0, 0, 0))
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, CHILD_ROUNDING)
            dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, 0)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 16, 14)
    return theme_tag


# ---------------------------------------------------------------------------
# Semantic helpers
# ---------------------------------------------------------------------------


def state_color(state_value: str) -> Tuple[int, int, int, int]:
    """Map a ``SessionState.value`` string to its indicator color."""
    return {
        "idle": STATE_IDLE,
        "preparing": STATE_PREPARING,
        "recording": STATE_RECORDING,
        "stopping": STATE_STOPPING,
        "stopped": STATE_STOPPED,
    }.get(state_value, TEXT_MUTED)


def series_color(index: int) -> Tuple[int, int, int, int]:
    """Pick a plot series color that cycles through the calibrated palette."""
    return PLOT_SERIES_COLORS[index % len(PLOT_SERIES_COLORS)]


def rgba_to_tuple(rgba: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    """Identity helper used by widgets to stay type-safe."""
    return rgba
