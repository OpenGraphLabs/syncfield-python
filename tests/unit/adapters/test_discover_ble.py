"""Unit tests for the BLE-based discover() classmethods.

Both OgloTactileStream and BLEImuGenericStream read from the shared
:func:`syncfield.discovery._ble.scan_peripherals` helper, so tests mock
that one function instead of patching bleak itself. This keeps the tests
fast (no real scan) and decoupled from the asyncio plumbing inside the
adapters.
"""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_bleak(monkeypatch):
    """Install a minimal fake ``bleak`` so the adapter modules import."""
    fake = MagicMock()
    monkeypatch.setitem(sys.modules, "bleak", fake)
    sys.modules.pop("syncfield.adapters.oglo_tactile", None)
    sys.modules.pop("syncfield.adapters.ble_imu", None)
    importlib.import_module("syncfield.adapters.oglo_tactile")
    importlib.import_module("syncfield.adapters.ble_imu")
    yield fake
    sys.modules.pop("syncfield.adapters.oglo_tactile", None)
    sys.modules.pop("syncfield.adapters.ble_imu", None)


def _peripheral(name: str, address: str) -> SimpleNamespace:
    """Build a BLEDevice-like object for use in mocked scan results."""
    return SimpleNamespace(name=name, address=address)


# ---------------------------------------------------------------------------
# OgloTactileStream.discover()
# ---------------------------------------------------------------------------


class TestOgloDiscover:
    def test_filters_by_name_substring(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        scan_result = [
            _peripheral("OGLO Left", "AA:BB:CC:DD:EE:01"),
            _peripheral("Random Speaker", "11:22:33:44:55:66"),
            _peripheral("OGLO Right", "AA:BB:CC:DD:EE:02"),
        ]

        with patch(
            "syncfield.discovery._ble.scan_peripherals", return_value=scan_result
        ):
            devices = OgloTactileStream.discover()

        assert len(devices) == 2
        assert all(d.adapter_type == "oglo_tactile" for d in devices)
        assert {d.display_name for d in devices} == {"OGLO Left", "OGLO Right"}
        assert all(d.accepts_output_dir is False for d in devices)
        assert all(d.kind == "sensor" for d in devices)

    def test_infers_hand_from_name(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        scan_result = [
            _peripheral("OGLO Left Glove", "AA:01"),
            _peripheral("OGLO Right Glove", "AA:02"),
            _peripheral("OGLO", "AA:03"),  # no hand suffix
        ]

        with patch(
            "syncfield.discovery._ble.scan_peripherals", return_value=scan_result
        ):
            devices = OgloTactileStream.discover()

        by_name = {d.display_name: d for d in devices}
        assert by_name["OGLO Left Glove"].construct_kwargs["hand"] == "left"
        assert by_name["OGLO Right Glove"].construct_kwargs["hand"] == "right"
        assert by_name["OGLO"].construct_kwargs["hand"] == "unknown"

    def test_address_is_populated_in_construct_kwargs(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        with patch(
            "syncfield.discovery._ble.scan_peripherals",
            return_value=[_peripheral("OGLO Right", "11:22:33:44:55:66")],
        ):
            (device,) = OgloTactileStream.discover()

        assert device.construct_kwargs["address"] == "11:22:33:44:55:66"

    def test_empty_scan_returns_empty_list(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        with patch(
            "syncfield.discovery._ble.scan_peripherals", return_value=[]
        ):
            assert OgloTactileStream.discover() == []

    def test_case_insensitive_match(self, mock_bleak):
        from syncfield.adapters.oglo_tactile import OgloTactileStream

        with patch(
            "syncfield.discovery._ble.scan_peripherals",
            return_value=[_peripheral("oglo_dev_42", "AA:BB")],
        ):
            assert len(OgloTactileStream.discover()) == 1


# ---------------------------------------------------------------------------
# BLEImuGenericStream.discover()
# ---------------------------------------------------------------------------


class TestBLEImuDiscover:
    def test_returns_all_non_oglo_peripherals(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuGenericStream

        scan_result = [
            _peripheral("BNO085 Dongle", "AA:01"),
            _peripheral("OGLO Right", "AA:02"),  # excluded
            _peripheral("Xsens DOT", "AA:03"),
        ]

        with patch(
            "syncfield.discovery._ble.scan_peripherals", return_value=scan_result
        ):
            devices = BLEImuGenericStream.discover()

        names = {d.display_name for d in devices}
        assert names == {"BNO085 Dongle", "Xsens DOT"}
        assert all(d.adapter_type == "ble_peripheral" for d in devices)
        assert all(d.accepts_output_dir is False for d in devices)
        assert all(d.kind == "sensor" for d in devices)

    def test_all_devices_carry_warning(self, mock_bleak):
        """Every generic BLE peripheral should be flagged as needing a
        characteristic_uuid — that's what causes scan_and_add to skip
        them so users get a clear INFO log."""
        from syncfield.adapters.ble_imu import BLEImuGenericStream

        with patch(
            "syncfield.discovery._ble.scan_peripherals",
            return_value=[_peripheral("BNO085", "AA:BB")],
        ):
            (device,) = BLEImuGenericStream.discover()

        assert len(device.warnings) == 1
        assert "characteristic_uuid" in device.warnings[0]

    def test_unnamed_peripheral_gets_fallback_label(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuGenericStream

        with patch(
            "syncfield.discovery._ble.scan_peripherals",
            return_value=[_peripheral("", "AA:BB:CC:DD:EE:FF")],
        ):
            (device,) = BLEImuGenericStream.discover()

        assert device.display_name.startswith("BLE peripheral")

    def test_empty_scan_returns_empty(self, mock_bleak):
        from syncfield.adapters.ble_imu import BLEImuGenericStream

        with patch(
            "syncfield.discovery._ble.scan_peripherals", return_value=[]
        ):
            assert BLEImuGenericStream.discover() == []
