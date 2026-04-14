"""Cross-platform WiFi network switching for Insta360 Go3S aggregation.

Each :class:`WifiSwitcher` subclass owns one OS-native CLI for switching
the host's primary WiFi interface between the user's lab network and
the camera's AP. The factory :func:`wifi_switcher_for_platform` returns
the right subclass based on ``sys.platform``.
"""
from __future__ import annotations

import abc
import json
import logging
import shutil
import subprocess
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)


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
    """macOS WiFi switcher using ``networksetup``.

    ``networksetup -setairportnetwork`` is surprisingly brittle on modern macOS:
    it can return 0 without actually switching, it can hang silently waiting
    for Location permission, and the DHCP lease on the camera AP takes a
    moment to settle. This impl mirrors the recorder's production-validated
    ``download_go3s_wifi.py`` flow: bounded timeouts, post-switch SSID
    verification, DHCP IP verification, and up to 3 retries.
    """

    #: Expected IP prefix the Go3S AP hands out over DHCP.
    _CAMERA_IP_PREFIX = "192.168.42."
    #: Seconds to wait for DHCP to assign an IP after the SSID switches.
    _DHCP_WAIT_SEC = 15
    #: How many times to retry the full switch on failure.
    _MAX_RETRIES = 2

    def _scan_visible_ssids(self) -> Optional[set[str]]:
        """Return the set of SSIDs macOS currently sees, or None on error.

        Uses ``system_profiler`` because the classic ``airport -s`` tool is
        deprecated in modern macOS. Parsing is best-effort — if the JSON
        schema differs across versions or the command fails, return None
        so callers can fall back to just trying the connect.
        """
        try:
            r = subprocess.run(
                ["system_profiler", "-json", "SPAirPortDataType"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return None
            data = json.loads(r.stdout)
        except Exception:
            return None

        ssids: set[str] = set()

        def _walk(obj: object) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k.endswith("_ssid_string") or k == "_name":
                        if isinstance(v, str) and v:
                            ssids.add(v)
                    _walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk(item)

        _walk(data)
        return ssids or None

    def current_ssid(self) -> Optional[str]:
        result = subprocess.run(
            ["networksetup", "-getairportnetwork", self.interface],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        line = (result.stdout or "").strip()
        prefix = "Current Wi-Fi Network: "
        if line.startswith(prefix):
            return line[len(prefix):].strip() or None
        return None

    def _get_interface_ip(self) -> Optional[str]:
        try:
            r = subprocess.run(
                ["ipconfig", "getifaddr", self.interface],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
        return None

    def connect(self, ssid: str, password: str) -> None:
        # Fast path: if we can enumerate visible SSIDs cleanly (no <redacted>
        # entries — which appear on macOS when Location Services isn't
        # granted to system_profiler), and the target isn't among them,
        # the camera's WiFi AP is simply OFF. networksetup would otherwise
        # spend tens of seconds fruitlessly retrying with an opaque error.
        visible = self._scan_visible_ssids()
        scan_is_useful = (
            visible is not None
            and len(visible) >= 3
            and not any("redacted" in s.lower() for s in visible)
        )
        if scan_is_useful:
            lowered = {s.lower() for s in visible}
            if ssid.lower() not in lowered:
                raise WifiSwitcherError(
                    f"Go3S WiFi AP {ssid!r} is not broadcasting. "
                    "On the camera: swipe down from the top of the "
                    "screen → WiFi → turn ON (or dock it in the Action "
                    "Pod, which keeps WiFi on). Then retry the "
                    "aggregation from the viewer."
                )

        last_diagnostic = ""
        for attempt in range(1, self._MAX_RETRIES + 1):
            logger.info(
                "[MacWifiSwitcher] setairportnetwork %s %r (attempt %d/%d)",
                self.interface, ssid, attempt, self._MAX_RETRIES,
            )
            try:
                result = subprocess.run(
                    [
                        "networksetup", "-setairportnetwork",
                        self.interface, ssid, password,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
            except subprocess.TimeoutExpired:
                last_diagnostic = (
                    "`networksetup -setairportnetwork` hung for 20s — likely "
                    "waiting for Location permission. Grant it in "
                    "System Settings → Privacy & Security → Location Services."
                )
                logger.warning("[MacWifiSwitcher] %s", last_diagnostic)
                continue

            combined = f"{result.stdout or ''} {result.stderr or ''}"

            # networksetup on modern macOS returns rc=0 even when the join
            # fails — the real signal is in stdout. Error -3925 / "Failed
            # to join" indicates the camera's AP is broadcasting but the
            # association was refused (typical cause: camera WiFi is in
            # standby — needs the camera screen awake to accept clients).
            if (
                "Failed to join" in combined
                or "-3925" in combined
                or "could not find" in combined.lower()
            ):
                last_diagnostic = (
                    f"networksetup reported {combined.strip()!r}. "
                    "The camera's WiFi AP refused the connection. "
                    "Wake the camera (tap its screen) so WiFi is actively "
                    "listening, then retry. If that doesn't help, toggle "
                    "WiFi OFF then ON in the camera settings."
                )
                logger.warning("[MacWifiSwitcher] %s", last_diagnostic)
                time.sleep(1)
                continue

            if result.returncode != 0:
                last_diagnostic = (
                    f"rc={result.returncode} stdout={result.stdout!r} "
                    f"stderr={result.stderr!r}"
                )
                logger.warning("[MacWifiSwitcher] %s", last_diagnostic)

            # networksetup can return 0 without actually switching — verify.
            time.sleep(2)
            actual = self.current_ssid()
            if actual != ssid:
                last_diagnostic = (
                    f"SSID verify failed: expected {ssid!r}, got {actual!r}. "
                    "If the camera AP isn't in range or its password is "
                    "wrong, macOS silently falls back to the last known "
                    "network."
                )
                logger.warning("[MacWifiSwitcher] %s", last_diagnostic)
                continue
            logger.info("[MacWifiSwitcher] SSID switched to %r", ssid)

            # Wait for DHCP to assign a 192.168.42.x IP.
            for i in range(self._DHCP_WAIT_SEC):
                time.sleep(1)
                ip = self._get_interface_ip()
                if ip and ip.startswith(self._CAMERA_IP_PREFIX):
                    logger.info(
                        "[MacWifiSwitcher] DHCP ok: iface=%s ip=%s",
                        self.interface, ip,
                    )
                    return
            last_diagnostic = (
                f"DHCP did not assign a {self._CAMERA_IP_PREFIX}x address "
                f"within {self._DHCP_WAIT_SEC}s (got {ip!r}). The camera "
                "AP is reachable but isn't serving DHCP — reboot the camera."
            )
            logger.warning("[MacWifiSwitcher] %s", last_diagnostic)

        raise WifiSwitcherError(
            f"Failed to switch WiFi to {ssid!r} after {self._MAX_RETRIES} "
            f"attempts.\n"
            f"Most common cause: the camera's WiFi AP is OFF.\n"
            f"  On the camera: swipe down from the top of the screen → "
            f"WiFi → turn ON. Or dock it in the Action Pod.\n"
            f"Last diagnostic: {last_diagnostic}"
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
