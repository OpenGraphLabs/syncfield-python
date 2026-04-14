"""Cross-platform WiFi network switching for Insta360 Go3S aggregation.

Each :class:`WifiSwitcher` subclass owns one OS-native CLI for switching
the host's primary WiFi interface between the user's lab network and
the camera's AP. The factory :func:`wifi_switcher_for_platform` returns
the right subclass based on ``sys.platform``.
"""
from __future__ import annotations

import abc
import shutil
import subprocess
import sys
from typing import Optional


class WifiSwitcherError(RuntimeError):
    """Raised when a WiFi switch / restore step cannot be completed."""


class WifiSwitcher(abc.ABC):
    def __init__(self, *, interface: str):
        self.interface = interface

    @abc.abstractmethod
    def current_ssid(self) -> Optional[str]: ...

    @abc.abstractmethod
    def connect(self, ssid: str, password: str) -> None: ...

    def restore(self, prev_ssid: Optional[str], prev_password: Optional[str] = None) -> None:
        """Default restore: reconnect to ``prev_ssid`` if it's known.

        ``prev_password`` is rarely required (the OS keychain usually
        remembers it) but supported for completeness.
        """
        if prev_ssid is None:
            return
        self.connect(prev_ssid, prev_password or "")


# ----- macOS -----

class MacWifiSwitcher(WifiSwitcher):
    def current_ssid(self) -> Optional[str]:
        result = subprocess.run(
            ["networksetup", "-getairportnetwork", self.interface],
            capture_output=True,
            text=True,
            check=False,
        )
        line = (result.stdout or "").strip()
        prefix = "Current Wi-Fi Network: "
        if line.startswith(prefix):
            return line[len(prefix):].strip() or None
        return None

    def connect(self, ssid: str, password: str) -> None:
        result = subprocess.run(
            ["networksetup", "-setairportnetwork", self.interface, ssid, password],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or "Could not" in (result.stdout or ""):
            raise WifiSwitcherError(
                f"networksetup failed: rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
            )


# ----- Linux -----

class LinuxWifiSwitcher(WifiSwitcher):
    def current_ssid(self) -> Optional[str]:
        # Prefer iwgetid which is universally available; nmcli works too.
        if shutil.which("iwgetid"):
            r = subprocess.run(
                ["iwgetid", self.interface, "--raw"],
                capture_output=True,
                text=True,
                check=False,
            )
            ssid = (r.stdout or "").strip()
            return ssid or None
        # Fallback to nmcli
        r = subprocess.run(
            ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in (r.stdout or "").splitlines():
            if line.startswith("yes:"):
                return line.split(":", 1)[1] or None
        return None

    def connect(self, ssid: str, password: str) -> None:
        result = subprocess.run(
            [
                "nmcli", "device", "wifi", "connect", ssid,
                "password", password,
                "ifname", self.interface,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise WifiSwitcherError(
                f"nmcli failed: rc={result.returncode} stderr={result.stderr!r}"
            )


# ----- Windows (stub) -----

class WindowsWifiSwitcher(WifiSwitcher):
    def current_ssid(self) -> Optional[str]:
        raise NotImplementedError(
            "Windows WiFi switching is not supported in v1; "
            "use BLE-only mode or run on macOS/Linux."
        )

    def connect(self, ssid: str, password: str) -> None:
        raise NotImplementedError(
            "Windows WiFi switching is not supported in v1; "
            "use BLE-only mode or run on macOS/Linux."
        )


# ----- Factory -----

def wifi_switcher_for_platform(*, interface: Optional[str] = None) -> WifiSwitcher:
    if sys.platform == "darwin":
        return MacWifiSwitcher(interface=interface or "en0")
    if sys.platform.startswith("linux"):
        return LinuxWifiSwitcher(interface=interface or "wlan0")
    if sys.platform.startswith("win"):
        return WindowsWifiSwitcher(interface=interface or "Wi-Fi")
    raise WifiSwitcherError(f"Unsupported platform: {sys.platform}")
