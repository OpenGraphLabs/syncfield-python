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


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.time.sleep", lambda *a, **k: None)
@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_mac_connect_invokes_setairportnetwork_and_verifies(mock_run):
    """Connect runs -setairportnetwork then verifies SSID + DHCP IP."""
    def fake_run(cmd, **kwargs):
        if "-setairportnetwork" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if "-getairportnetwork" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout="Current Wi-Fi Network: Go3S-CAFEBABE.OSC\n",
                stderr="",
            )
        if cmd[0] == "ipconfig":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="192.168.42.2\n", stderr="",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="?")
    mock_run.side_effect = fake_run

    sw = MacWifiSwitcher(interface="en0")
    sw.connect("Go3S-CAFEBABE.OSC", "88888888")

    set_call = next(
        c for c in mock_run.call_args_list if "-setairportnetwork" in c.args[0]
    )
    cmd = set_call.args[0]
    assert cmd[:2] == ["networksetup", "-setairportnetwork"]
    assert cmd[2] == "en0"
    assert cmd[3] == "Go3S-CAFEBABE.OSC"
    assert cmd[4] == "88888888"


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.time.sleep", lambda *a, **k: None)
@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_mac_connect_retries_and_raises_when_ssid_never_switches(mock_run):
    """If SSID verify never matches expected, raise after MAX_RETRIES."""
    def fake_run(cmd, **kwargs):
        if "-setairportnetwork" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if "-getairportnetwork" in cmd:
            # Still on the OLD network — verify fails.
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout="Current Wi-Fi Network: LabWiFi\n",
                stderr="",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
    mock_run.side_effect = fake_run

    sw = MacWifiSwitcher(interface="en0")
    with pytest.raises(WifiSwitcherError, match="after 3 attempts"):
        sw.connect("does-not-exist", "x")


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.time.sleep", lambda *a, **k: None)
@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_mac_connect_raises_when_dhcp_does_not_assign_camera_ip(mock_run):
    """SSID switches OK but DHCP never hands out 192.168.42.x."""
    def fake_run(cmd, **kwargs):
        if "-setairportnetwork" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if "-getairportnetwork" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout="Current Wi-Fi Network: Go3S-CAFEBABE.OSC\n",
                stderr="",
            )
        if cmd[0] == "ipconfig":
            # Stuck at the lab IP even after the switch.
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="10.0.1.55\n", stderr="",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
    mock_run.side_effect = fake_run

    sw = MacWifiSwitcher(interface="en0")
    with pytest.raises(WifiSwitcherError, match="DHCP"):
        sw.connect("Go3S-CAFEBABE.OSC", "88888888")


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
