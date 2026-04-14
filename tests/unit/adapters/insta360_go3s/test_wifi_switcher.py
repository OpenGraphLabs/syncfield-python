import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from syncfield.adapters.insta360_go3s.wifi.switcher import (
    LinuxWifiSwitcher,
    MacWifiSwitcher,
    WifiSwitcher,
    WifiSwitcherError,
    WindowsWifiSwitcher,
    wifi_switcher_for_platform,
)


def test_abc_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        WifiSwitcher()  # type: ignore[abstract]


# ----- macOS -----

@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_mac_current_ssid_parses_networksetup_output(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="Current Wi-Fi Network: LabWiFi\n",
        stderr="",
    )
    sw = MacWifiSwitcher(interface="en0")
    assert sw.current_ssid() == "LabWiFi"


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_mac_current_ssid_returns_none_when_disconnected(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="You are not associated with an AirPort network.\n",
        stderr="",
    )
    sw = MacWifiSwitcher(interface="en0")
    assert sw.current_ssid() is None


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_mac_connect_invokes_setairportnetwork(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )
    sw = MacWifiSwitcher(interface="en0")
    sw.connect("Go3S-CAFEBABE.OSC", "88888888")
    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "networksetup"
    assert "-setairportnetwork" in cmd
    assert "en0" in cmd
    assert "Go3S-CAFEBABE.OSC" in cmd
    assert "88888888" in cmd


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_mac_connect_failure_raises(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="Could not find network"
    )
    sw = MacWifiSwitcher(interface="en0")
    with pytest.raises(WifiSwitcherError):
        sw.connect("does-not-exist", "x")


# ----- Linux -----

@patch("syncfield.adapters.insta360_go3s.wifi.switcher.shutil.which", return_value="/usr/bin/iwgetid")
@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_linux_current_ssid_parses_iwgetid(mock_run, mock_which):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="LabWiFi\n", stderr=""
    )
    sw = LinuxWifiSwitcher(interface="wlan0")
    assert sw.current_ssid() == "LabWiFi"


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_linux_connect_invokes_nmcli(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )
    sw = LinuxWifiSwitcher(interface="wlan0")
    sw.connect("Go3S-CAFEBABE.OSC", "88888888")
    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "nmcli"
    assert "wlan0" in cmd
    assert "Go3S-CAFEBABE.OSC" in cmd
    assert "88888888" in cmd


# ----- Windows stub -----

def test_windows_raises_not_implemented():
    sw = WindowsWifiSwitcher(interface="Wi-Fi")
    with pytest.raises(NotImplementedError):
        sw.connect("x", "y")


# ----- Factory -----

@patch("syncfield.adapters.insta360_go3s.wifi.switcher.sys.platform", "darwin")
def test_factory_returns_mac_on_darwin():
    sw = wifi_switcher_for_platform()
    assert isinstance(sw, MacWifiSwitcher)


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.sys.platform", "linux")
def test_factory_returns_linux_on_linux():
    sw = wifi_switcher_for_platform()
    assert isinstance(sw, LinuxWifiSwitcher)


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.sys.platform", "win32")
def test_factory_returns_windows_on_win32():
    sw = wifi_switcher_for_platform()
    assert isinstance(sw, WindowsWifiSwitcher)
