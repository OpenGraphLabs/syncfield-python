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

    # dns-sd -L expects service_type WITHOUT the trailing domain
    # (e.g. "_syncfield._tcp"), with the domain as a separate arg
    # ("local."). Passing the fully-qualified "_syncfield._tcp.local."
    # makes dns-sd silently produce no output, so we strip it here.
    short_service_type = service_type.rstrip(".")
    if short_service_type.endswith(".local"):
        short_service_type = short_service_type[: -len(".local")]

    cmd = ["dns-sd", "-L", instance, short_service_type, "local."]
    logger.debug("dns-sd fallback: running %s", cmd)
    try:
        proc = subprocess.Popen(
            cmd,
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

        # Resolve hostname -> IPv4. dns-sd output sometimes carries a
        # doubled .local suffix when the Mac's HostName is already set
        # to something ending in .local (e.g. "Jerryui-MacBookPro.local"
        # → "Jerryui-MacBookPro.local.local."). Try several normalised
        # variants in order; the first that resolves wins.
        addr_packed = _resolve_hostname_to_packed_ipv4(host, timeout=timeout)
        if addr_packed is None:
            logger.debug(
                "dns-sd fallback: could not resolve hostname %r to IPv4 "
                "(tried gethostbyname variants + dns-sd -G)",
                host,
            )
            return None

        return _ResolvedInfo(
            port=port,
            properties=txt_props,
            _addresses=[addr_packed],
        )
    finally:
        _terminate_subprocess(proc)


def _hostname_variants(host: str) -> List[str]:
    """Yield reasonable normalisations of an mDNS hostname.

    dns-sd's ``-L`` output occasionally produces ``host.local.local.``
    when the Mac's HostName already ends with ``.local``. macOS's
    ``getaddrinfo`` won't resolve the doubled form, but it WILL resolve
    the deduplicated one. Return the original (minus trailing dot) plus
    the dedup'd variant plus the bare hostname so the caller can probe
    each.
    """
    raw = host.rstrip(".")
    seen: List[str] = []

    def _add(candidate: str) -> None:
        if candidate and candidate not in seen:
            seen.append(candidate)

    _add(raw)

    # Collapse repeated trailing ``.local`` segments to a single one.
    if raw.endswith(".local.local"):
        _add(raw[: -len(".local")])
    elif raw.endswith(".local.local."):
        _add(raw[: -len(".local.")])

    # Bare host (no .local suffix). Can succeed if the OS resolver picks
    # up the .local appropriately or if /etc/hosts has the bare form.
    if raw.endswith(".local"):
        _add(raw[: -len(".local")])

    return seen


def _resolve_hostname_to_packed_ipv4(
    host: str, *, timeout: float
) -> Optional[bytes]:
    """Resolve a hostname to a packed 4-byte IPv4 address, or None.

    Tries each normalised variant via ``socket.gethostbyname`` first.
    Falls back to ``dns-sd -G v4`` (also via macOS's mDNSResponder) if
    every variant fails — useful when ``getaddrinfo`` for ``.local.``
    names doesn't end up calling mDNSResponder for some reason.
    """
    for candidate in _hostname_variants(host):
        try:
            addr_str = socket.gethostbyname(candidate)
            return socket.inet_aton(addr_str)
        except (socket.gaierror, OSError):
            continue

    # Final fallback: ask dns-sd directly. -G v4 keeps emitting on
    # multiple interfaces; we just need the first valid IPv4.
    for candidate in _hostname_variants(host):
        addr_str = _resolve_via_dns_sd_g(candidate, timeout=timeout)
        if addr_str:
            try:
                return socket.inet_aton(addr_str)
            except OSError:
                continue
    return None


def _resolve_via_dns_sd_g(
    hostname: str, *, timeout: float
) -> Optional[str]:
    """Run ``dns-sd -G v4 <hostname>.`` and return the first IPv4 line."""
    cmd = ["dns-sd", "-G", "v4", hostname.rstrip(".") + "."]
    logger.debug("dns-sd fallback: running %s", cmd)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, FileNotFoundError):
        return None

    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select(
                    [proc.stdout], [], [], min(remaining, 0.25)
                )
            except (ValueError, OSError):
                break
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            # Format: ``HH:MM:SS.mmm Add 2 14 hostname.local. 192.168.1.5``
            tokens = line.strip().split()
            if not tokens:
                continue
            for token in reversed(tokens):
                # Pick the token that parses as a valid IPv4.
                try:
                    socket.inet_aton(token)
                    return token
                except OSError:
                    continue
        return None
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
