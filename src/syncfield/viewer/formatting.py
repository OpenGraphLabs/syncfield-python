"""Tiny formatting helpers used across viewer widgets.

Keeping these in one module (instead of sprinkling inline ``f''``-strings
across every widget) means we have a single place to change the display
rules — e.g. switch from ``00:12.345`` to ``12.3s`` for the timer.
"""

from __future__ import annotations

from typing import Optional


def format_elapsed(seconds: float) -> str:
    """Format a duration as ``MM:SS.mmm``."""
    if seconds < 0:
        seconds = 0.0
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    whole = int(remainder)
    millis = int(round((remainder - whole) * 1000))
    if millis >= 1000:
        millis = 999
    return f"{minutes:02d}:{whole:02d}.{millis:03d}"


def format_hz(hz: float) -> str:
    """Format a frequency for display as ``29.9 Hz``."""
    if hz <= 0:
        return "—"
    if hz >= 100:
        return f"{hz:.0f} Hz"
    return f"{hz:.1f} Hz"


def format_count(count: int) -> str:
    """Format a frame/sample count with thousands separators."""
    return f"{count:,}"


def format_ns_ago(ns: Optional[int], now_ns: int) -> str:
    """Format ``now_ns - ns`` as a human-readable 'Xms ago' string."""
    if ns is None:
        return "—"
    delta_ms = (now_ns - ns) / 1e6
    if delta_ms < 0:
        delta_ms = 0.0
    if delta_ms < 1000:
        return f"{delta_ms:.0f} ms ago"
    if delta_ms < 60_000:
        return f"{delta_ms / 1000:.1f} s ago"
    return f"{delta_ms / 60_000:.1f} min ago"


def format_path_tail(path: str, max_chars: int = 60) -> str:
    """Truncate a long path from the left so the tail (episode id) stays visible."""
    if len(path) <= max_chars:
        return path
    return "…" + path[-(max_chars - 1):]


def format_chirp_pair(
    chirp_start_ns: Optional[int],
    chirp_stop_ns: Optional[int],
) -> str:
    """Format the chirp start/stop pair for the session clock panel."""
    if chirp_start_ns is None:
        return "pending"
    start_s = chirp_start_ns / 1e9
    if chirp_stop_ns is None:
        return f"start @ {start_s:.3f}s"
    span_ms = (chirp_stop_ns - chirp_start_ns) / 1e6
    return f"start + {span_ms:.0f} ms span"


def state_label(state_value: str) -> str:
    """Uppercase a session state for the header chip."""
    return state_value.upper() if state_value else ""
