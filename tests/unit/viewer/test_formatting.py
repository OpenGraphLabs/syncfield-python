"""Unit tests for the viewer's formatting helpers.

These tests exercise pure-Python logic that has no DearPyGui dependency,
so they run in any environment.
"""

from __future__ import annotations

import pytest

pytest.importorskip("dearpygui.dearpygui")

from syncfield.viewer.widgets.formatting import (
    format_chirp_pair,
    format_count,
    format_elapsed,
    format_hz,
    format_ns_ago,
    format_path_tail,
    state_label,
)


class TestFormatElapsed:
    def test_zero(self):
        assert format_elapsed(0) == "00:00.000"

    def test_sub_second(self):
        assert format_elapsed(0.123) == "00:00.123"

    def test_full_minutes(self):
        assert format_elapsed(65.5) == "01:05.500"

    def test_large(self):
        assert format_elapsed(600.001) == "10:00.001"

    def test_negative_clamped_to_zero(self):
        assert format_elapsed(-5.0) == "00:00.000"

    def test_millisecond_overflow_clamped(self):
        """Floating point rounding to 1000 must not produce '00.1000'."""
        # 59.9995 rounds to 1000 ms → must clamp to 999
        value = format_elapsed(59.9999999)
        minutes, rest = value.split(":")
        assert len(rest) == 6  # "SS.mmm"
        assert int(rest.split(".")[1]) <= 999


class TestFormatHz:
    def test_zero(self):
        assert format_hz(0) == "—"

    def test_low(self):
        assert format_hz(29.9) == "29.9 Hz"

    def test_high(self):
        assert format_hz(100.7) == "101 Hz"


class TestFormatCount:
    def test_small(self):
        assert format_count(5) == "5"

    def test_thousands(self):
        assert format_count(1234) == "1,234"

    def test_millions(self):
        assert format_count(1_234_567) == "1,234,567"


class TestFormatNsAgo:
    def test_none(self):
        assert format_ns_ago(None, 0) == "—"

    def test_millis(self):
        assert format_ns_ago(0, 500_000_000) == "500 ms ago"

    def test_seconds(self):
        assert format_ns_ago(0, 2_500_000_000) == "2.5 s ago"

    def test_minutes(self):
        assert format_ns_ago(0, 120_000_000_000).endswith("min ago")

    def test_never_negative(self):
        assert format_ns_ago(100, 50) == "0 ms ago"


class TestFormatPathTail:
    def test_short_path_unchanged(self):
        path = "/tmp/data"
        assert format_path_tail(path) == path

    def test_long_path_truncated_from_left(self):
        path = "/" + "a" * 100
        out = format_path_tail(path, max_chars=20)
        assert out.startswith("…")
        assert len(out) == 20


class TestFormatChirpPair:
    def test_pending(self):
        assert format_chirp_pair(None, None) == "pending"

    def test_start_only(self):
        assert format_chirp_pair(1_234_000_000, None).startswith("start @ ")

    def test_start_and_stop(self):
        out = format_chirp_pair(1_000_000_000, 1_500_000_000)
        assert "500 ms" in out


class TestStateLabel:
    def test_empty(self):
        assert state_label("") == ""

    def test_uppercase(self):
        assert state_label("recording") == "RECORDING"
