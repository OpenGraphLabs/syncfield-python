"""Which glove is which. Pure — no bleak, no asyncio, no radio."""

from __future__ import annotations

import pytest

from syncfield.adapters.oglo.selection import (
    AmbiguousGloveError,
    GloveCandidate,
    select_glove,
    side_of,
)


class _Device:
    """Stand-in for bleak's ``BLEDevice`` — only ``address`` is ever read."""

    def __init__(self, address: str) -> None:
        self.address = address

    def __repr__(self) -> str:
        return f"_Device({self.address})"


def candidate(name: str, address: str, local_name: str = "") -> GloveCandidate:
    return GloveCandidate(_Device(address), [name, local_name])


# The two peripherals actually on the air at the og-skill dev board, 2026-07-10.
LEFT = candidate("OGLO_LEFT_TEST01", "28:84:85:BB:60:ED")
RIGHT = candidate("OGLO RIGHT", "28:84:85:BB:AF:3D")
OLD_RIGHT = candidate("OGLO_V2_RIGHT", "28:84:85:BB:76:C5")
PHONE = candidate("Some Phone", "AA:BB:CC:DD:EE:FF")


class TestSideOf:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("OGLO LEFT", "left"),
            ("OGLO_LEFT_TEST01", "left"),
            ("oglo left", "left"),
            ("OGLO RIGHT", "right"),
            ("OGLO_V2_RIGHT", "right"),
            ("OGLO-RIGHT-02", "right"),
            ("OGLO-RIGHT-HAND", "right"),
        ],
    )
    def test_separators_suffixes_and_case_do_not_matter(self, name, expected):
        assert side_of(name) == expected

    @pytest.mark.parametrize("name", ["OGLO_BRIGHT", "OGLO_RIGHTHAND"])
    def test_a_side_must_be_a_whole_token(self, name):
        # "BRIGHT" and "RIGHTHAND" both contain "RIGHT" as a substring. Neither
        # yields it as a token, so neither is a right glove.
        assert side_of(name) is None

    def test_a_non_oglo_peripheral_is_not_a_glove(self):
        assert side_of("LEFT SPEAKER") is None

    def test_an_oglo_without_a_side_identifies_nothing(self):
        assert side_of("OGLO") is None

    def test_a_name_claiming_both_sides_identifies_nothing(self):
        assert side_of("OGLO_LEFT_RIGHT") is None

    def test_an_empty_name_identifies_nothing(self):
        assert side_of("") is None


class TestSelectByHand:
    def test_the_left_stream_gets_the_left_glove(self):
        got = select_glove([PHONE, RIGHT, LEFT], ble_name="oglo", hand="left")
        assert got is LEFT.device

    def test_the_right_stream_gets_the_right_glove(self):
        got = select_glove([PHONE, RIGHT, LEFT], ble_name="oglo", hand="right")
        assert got is RIGHT.device

    def test_the_bug_this_module_exists_to_fix(self):
        """Before this, both streams took the first name containing "oglo" and
        `hand` was ignored entirely. With both gloves on — the only way the
        tactile setups ever run — left and right resolved to the same
        peripheral, and dict order picked it. One hand's taxels landed in the
        other hand's file, and nothing downstream could tell."""
        scan = [RIGHT, LEFT]  # right advertises first
        left = select_glove(scan, ble_name="oglo", hand="left")
        right = select_glove(scan, ble_name="oglo", hand="right")
        assert left is not right
        assert left is LEFT.device
        assert right is RIGHT.device

    def test_a_missing_hand_selects_nothing(self):
        assert select_glove([LEFT], ble_name="oglo", hand="right") is None

    def test_a_side_less_oglo_never_satisfies_a_hand(self):
        assert select_glove([candidate("OGLO", "1")], ble_name="oglo", hand="left") is None

    def test_local_name_carries_the_side_when_the_device_name_does_not(self):
        # bleak surfaces the advertisement's local_name separately; either may
        # be the one that names the glove.
        c = candidate("", "11:22", local_name="OGLO LEFT")
        assert select_glove([c], ble_name="oglo", hand="left") is c.device


class TestAmbiguityIsFatal:
    def test_two_gloves_claiming_the_same_hand_raise(self):
        spare = candidate("OGLO_LEFT_SPARE", "99:99:99:99:99:99")
        with pytest.raises(AmbiguousGloveError) as exc:
            select_glove([LEFT, spare, RIGHT], ble_name="oglo", hand="left")

        message = str(exc.value)
        # The message must name both candidates, or nobody can act on it.
        assert LEFT.device.address in message
        assert spare.device.address in message
        assert "address=" in message, "must say how to disambiguate"

    def test_the_unambiguous_hand_still_resolves(self):
        spare = candidate("OGLO_LEFT_SPARE", "99:99:99:99:99:99")
        assert select_glove([LEFT, spare, RIGHT], ble_name="oglo", hand="right") is RIGHT.device

    def test_two_matching_peripherals_with_an_unknown_hand_also_raise(self):
        """A caller that cannot say which glove it wants has no defensible way
        to choose between two."""
        with pytest.raises(AmbiguousGloveError):
            select_glove([LEFT, RIGHT], ble_name="oglo", hand="unknown")

    def test_one_match_with_an_unknown_hand_is_fine(self):
        assert select_glove([LEFT, PHONE], ble_name="oglo", hand="unknown") is LEFT.device


class TestNameFilter:
    def test_the_filter_is_applied_before_the_hand(self):
        decoy = candidate("NOTGLOVE LEFT", "77:77")
        assert select_glove([decoy], ble_name="oglo", hand="left") is None

    def test_the_filter_is_case_insensitive_and_substring(self):
        assert select_glove([LEFT], ble_name="OgLo", hand="left") is LEFT.device

    def test_nothing_on_the_air_selects_nothing(self):
        assert select_glove([], ble_name="oglo", hand="left") is None

    def test_a_more_specific_filter_still_works(self):
        assert select_glove([LEFT, RIGHT, OLD_RIGHT], ble_name="oglo_v2", hand="right") is (
            OLD_RIGHT.device
        )
