"""System font loader for the SyncField desktop viewer.

DearPyGui's built-in font (ProggyClean) is a 13 px ASCII bitmap — it
looks dated and, more importantly, it renders every non-ASCII glyph as
``?``. The viewer uses a handful of geometric symbols (``●``, ``■``,
``→``, ``—``) plus the occasional warning glyph, so a font with the
extended Latin ranges is a correctness requirement, not just polish.

This module walks a small list of platform-appropriate system font paths
and loads the first one it finds into the DearPyGui font registry at the
typographic scale the layout expects. The returned :class:`FontRegistry`
hands out tags per display role — body text, muted text, display sizes,
monospace for timestamps and device ids.

If no system font is found the module falls back gracefully: the
returned registry's fields are all ``None`` and the caller keeps the
DearPyGui default font. The UI will still work; exotic glyphs will just
render as placeholders.
"""

from __future__ import annotations

import logging
import os
import platform
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry returned to the viewer app
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FontRegistry:
    """Bundle of DearPyGui font tags keyed by display role.

    All fields default to ``None`` so callers can treat "font missing"
    the same way as "don't override this element's font" — no special
    casing in widget code.
    """

    ui_sm: Optional[int] = None     # 12 px — small labels, muted captions
    ui: Optional[int] = None        # 14 px — body text (bound globally)
    ui_md: Optional[int] = None     # 16 px — section titles, card headers
    ui_lg: Optional[int] = None     # 22 px — app title, prominent timers
    mono: Optional[int] = None      # 14 px — device ids, timestamps


# ---------------------------------------------------------------------------
# Platform font catalog
# ---------------------------------------------------------------------------

# Ordered by preference. First existing file wins.
_SANS_CANDIDATES = {
    "Darwin": [
        # San Francisco Pro — Apple's system UI font. Single-file .ttf
        # so DearPyGui can load it without .ttc collection handling.
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/SFCompact.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ],
    "Linux": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
    ],
    "Windows": [
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ],
}

_MONO_CANDIDATES = {
    "Darwin": [
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
    ],
    "Linux": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ],
    "Windows": [
        r"C:\Windows\Fonts\consola.ttf",
        r"C:\Windows\Fonts\lucon.ttf",
    ],
}


def _find_first_existing(candidates: List[str]) -> Optional[str]:
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


# ---------------------------------------------------------------------------
# Typography scale
# ---------------------------------------------------------------------------

# Keyed by the FontRegistry field name so ``load_fonts`` can splat it in.
_SIZE_SCALE = {
    "ui_sm": 13,   # captions, muted labels
    "ui": 15,      # body text (bound globally)
    "ui_md": 17,   # card titles, controls
    "ui_lg": 24,   # app title, prominent display
}


# ---------------------------------------------------------------------------
# Unicode ranges needed by viewer glyphs
# ---------------------------------------------------------------------------

# DearPyGui's default range hint covers Basic Latin + Latin-1 Supplement.
# Everything else the viewer uses lives in these blocks — we rasterize
# only what we need so the font atlas stays small.
_EXTRA_RANGES = (
    (0x2000, 0x206F),   # General Punctuation — em dash, ellipsis, bullet
    (0x2190, 0x21FF),   # Arrows              — →
    (0x25A0, 0x25FF),   # Geometric Shapes    — ● ■ ◐ □
    (0x2600, 0x26FF),   # Miscellaneous Symbols — ⚠
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def load_fonts() -> FontRegistry:
    """Load viewer fonts into the DearPyGui font registry.

    Must be called **after** ``dpg.create_context()`` and **before** the
    first widget is built. Returns a :class:`FontRegistry` of font tags
    the caller can pass to ``dpg.bind_font`` (for the global default)
    and ``dpg.bind_item_font`` (for per-widget overrides like the app
    title or monospace device ids).

    When no system font is available the returned registry is empty and
    the viewer falls back to the DearPyGui built-in font.
    """
    import dearpygui.dearpygui as dpg

    system = platform.system()
    sans_path = _find_first_existing(_SANS_CANDIDATES.get(system, []))
    mono_path = _find_first_existing(_MONO_CANDIDATES.get(system, []))

    if sans_path is None:
        logger.warning(
            "viewer: no system UI font found on %s; exotic glyphs will "
            "render as '?' placeholders. Install a TrueType font such as "
            "DejaVu Sans or Noto Sans to fix.",
            system,
        )
        return FontRegistry()

    logger.debug(
        "viewer: loading UI font %s (mono=%s)", sans_path, mono_path or "none"
    )

    tags: dict = {}
    with dpg.font_registry():
        for role, size in _SIZE_SCALE.items():
            tag = _load_sized_font(dpg, sans_path, size)
            if tag is not None:
                tags[role] = tag

        if mono_path is not None:
            mono_tag = _load_sized_font(dpg, mono_path, 15)
            if mono_tag is not None:
                tags["mono"] = mono_tag

    return FontRegistry(**tags)


def _load_sized_font(dpg_module, font_path: str, size: int) -> Optional[int]:
    """Load one ``font_path`` at ``size`` px and register the extra ranges.

    Returns the font tag on success or ``None`` if DearPyGui rejects the
    file (corrupt file, unreadable collection index, etc.). Never raises
    — a bad font file should never crash the viewer.
    """
    try:
        with dpg_module.font(font_path, size) as font_tag:
            dpg_module.add_font_range_hint(dpg_module.mvFontRangeHint_Default)
            for start, end in _EXTRA_RANGES:
                dpg_module.add_font_range(start, end)
        return font_tag
    except Exception as exc:
        logger.debug("viewer: failed to load %s @ %dpx: %s", font_path, size, exc)
        return None
