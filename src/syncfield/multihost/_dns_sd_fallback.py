"""macOS-only fallback: resolve mDNS SRV/TXT via subprocess(`dns-sd`).

python-zeroconf's `get_service_info` reliably returns None on macOS
because the OS-level mDNSResponder daemon (which always runs and owns
port 5353 for AirDrop/AirPlay/Continuity Camera) intercepts unicast
replies to python-zeroconf's resolution queries. The browser still
receives multicast PTR announcements (so `add_service` callbacks fire),
but SRV/TXT resolution times out.

This module shells out to macOS's native `dns-sd` command, which talks
directly to mDNSResponder and gets SRV/TXT correctly. The output is
parsed and converted into a `_ResolvedInfo` value object that exposes
the same attributes (`port`, `properties`, `parsed_addresses`,
`addresses`) the rest of `SessionBrowser._refresh` already consumes.

This module is a no-op on every non-Darwin platform; the fallback is
gated by `platform.system() == "Darwin"` at the call site.
"""

from __future__ import annotations

import logging
import platform
import select
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResolvedInfo:
    """Subset of `zeroconf.ServiceInfo` the browser actually uses."""

    port: int
    properties: Dict[bytes, bytes]
    _addresses: List[bytes]  # packed IPv4 (4-byte bytes) per address

    def parsed_addresses(self) -> List[str]:
        return [socket.inet_ntoa(a) for a in self._addresses]

    @property
    def addresses(self) -> List[bytes]:
        return list(self._addresses)


def is_macos() -> bool:
    return platform.system() == "Darwin"


def resolve_via_dns_sd(
    name: str,
    service_type: str,
    *,
    timeout: float = 5.0,
) -> Optional[_ResolvedInfo]:
    """Resolve a service via macOS native `dns-sd -L`. Returns None on failure.

    Args:
        name: Full service-instance name as zeroconf delivers it, e.g.
            ``"lab_session--mac_a._syncfield._tcp.local."``. The bare
            instance portion is extracted from this.
        service_type: e.g. ``"_syncfield._tcp.local."``.
        timeout: Wall-clock budget for the whole subprocess interaction.

    Only runs on macOS — other platforms return None immediately so
    callers can use this unconditionally as a fallback.
    """
    if not is_macos():
        return None

    instance = _strip_service_suffix(name, service_type)
    if not instance:
        logger.debug("dns-sd fallback: cannot extract instance from %r", name)
        return None

    try:
        proc = subprocess.Popen(
            ["dns-sd", "-L", instance, service_type, "local."],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, FileNotFoundError) as exc:
        logger.debug("dns-sd fallback: failed to spawn subprocess: %s", exc)
        return None

    try:
        host, port, txt_props = _read_dns_sd_output(proc, timeout)
        if not (host and port):
            logger.debug(
                "dns-sd fallback: did not receive both host and port within "
                "%.1fs for %s", timeout, instance,
            )
            return None

        # Resolve hostname -> IPv4. macOS's getaddrinfo also goes through
        # mDNSResponder for ``.local.`` names, so this is reliable here.
        try:
            addr_str = socket.gethostbyname(host.rstrip("."))
            addr_packed = socket.inet_aton(addr_str)
        except (socket.gaierror, OSError) as exc:
            logger.debug(
                "dns-sd fallback: hostname resolution failed for %s: %s",
                host, exc,
            )
            return None

        return _ResolvedInfo(
            port=port,
            properties=txt_props,
            _addresses=[addr_packed],
        )
    finally:
        _terminate_subprocess(proc)


def _strip_service_suffix(name: str, service_type: str) -> Optional[str]:
    """``"foo._syncfield._tcp.local."`` + ``"_syncfield._tcp.local."`` -> ``"foo"``."""
    suffix = "." + service_type.rstrip(".")
    n = name.rstrip(".")
    if n.endswith(suffix):
        return n[: -len(suffix)]
    # Already-bare instance name? Tolerate.
    if "." not in n:
        return n
    return None


def _read_dns_sd_output(
    proc: subprocess.Popen, timeout: float
) -> "tuple[Optional[str], Optional[int], Dict[bytes, bytes]]":
    """Read until we have host+port+TXT, or until the timeout elapses.

    Output format example::

        Lookup lab_session--mac_a._syncfield._tcp.local.
        DATE: ---Mon 13 Apr 2026---
        16:30:00.000  ...STARTING...
        16:30:00.012  lab_session--mac_a._syncfield._tcp.local. can be reached at host.local.:7878 (interface 14)
         host_id=mac_a session_id=lab_session sdk_version=0.3.6 chirp_enabled=1
    """
    deadline = time.monotonic() + timeout
    host: Optional[str] = None
    port: Optional[int] = None
    txt_props: Dict[bytes, bytes] = {}

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 0.25))
        except (ValueError, OSError):
            break
        if not ready:
            continue

        line = proc.stdout.readline()
        if not line:
            # subprocess closed stdout — done.
            break

        if "can be reached at" in line:
            # Extract "host:port" — the token right after "at".
            tail = line.split("can be reached at", 1)[1].strip()
            host_port = tail.split()[0] if tail else ""
            if ":" in host_port:
                h, p = host_port.rsplit(":", 1)
                try:
                    port = int(p)
                    host = h
                except ValueError:
                    pass
        elif "=" in line:
            # TXT record line. Tokens like ``key=value`` separated by spaces.
            for token in line.strip().split():
                if "=" not in token:
                    continue
                k, v = token.split("=", 1)
                # dns-sd may quote values that contain spaces.
                if v.startswith('"') and v.endswith('"') and len(v) >= 2:
                    v = v[1:-1]
                try:
                    txt_props[k.encode("utf-8")] = v.encode("utf-8")
                except UnicodeEncodeError:
                    pass

        if host and port and txt_props:
            return host, port, txt_props

    return host, port, txt_props


def _terminate_subprocess(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=1.0)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=0.5)
        except Exception:
            pass
