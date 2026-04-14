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
import os
import platform
import pty
import select
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResolvedInfo:
    """Subset of `zeroconf.ServiceInfo` the browser actually uses.

    ``_addresses`` may be empty if hostname-to-IPv4 resolution failed
    (e.g. on a network where unicast UDP is blocked but mDNS PTR
    multicast still flows). In that case ``hostname`` carries the
    .local hostname which httpx + macOS getaddrinfo can resolve at
    request time via mDNSResponder.
    """

    port: int
    properties: Dict[bytes, bytes]
    _addresses: List[bytes]  # packed IPv4 (4-byte bytes) per address
    hostname: Optional[str] = None  # normalised .local hostname

    def parsed_addresses(self) -> List[str]:
        ips = [socket.inet_ntoa(a) for a in self._addresses]
        if ips:
            return ips
        # Hostname-only fallback — return the .local hostname so the
        # browser's resolved_address gets populated with something
        # httpx can dial.
        return [self.hostname] if self.hostname else []

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
    # Spawn under a pty so dns-sd line-buffers its stdout. With a plain
    # subprocess.PIPE, stdio block-buffers (~8KB) and a single resolve's
    # 100-byte output never reaches our select() until the process exits
    # — and dns-sd never exits on its own.
    pty_resources = _spawn_with_pty(cmd)
    if pty_resources is None:
        return None
    proc, master_fd = pty_resources

    try:
        host, port, txt_props = _read_dns_sd_output_pty(master_fd, timeout)
        if not (host and port):
            logger.debug(
                "dns-sd fallback: did not receive both host and port within "
                "%.1fs for %s", timeout, instance,
            )
            return None

        # Try to resolve the hostname to an IPv4 address. dns-sd output
        # sometimes carries a doubled .local suffix when the Mac's HostName
        # is already set to something ending in .local. We attempt several
        # normalised hostname variants and a dns-sd -G subprocess. If
        # everything fails, we still return success with the *hostname*
        # itself stored on the value object — httpx + macOS getaddrinfo
        # can resolve a .local hostname directly via mDNSResponder, so the
        # control-plane URLs still work even without a packed IPv4.
        normalized_hostname = _normalize_hostname(host)
        addr_packed = _resolve_hostname_to_packed_ipv4(host, timeout=timeout)

        return _ResolvedInfo(
            port=port,
            properties=txt_props,
            _addresses=[addr_packed] if addr_packed is not None else [],
            hostname=normalized_hostname,
        )
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        _terminate_subprocess(proc)


def _normalize_hostname(host: str) -> str:
    """Return a sensible single-.local hostname for use in URLs.

    dns-sd's ``-L`` output often shows ``host.local.local.`` when the
    Mac's HostName already ends in ``.local``. macOS httpx /
    getaddrinfo wants ``host.local`` (single .local, no trailing dot).
    """
    raw = host.rstrip(".")
    while raw.endswith(".local.local"):
        raw = raw[: -len(".local")]
    return raw


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


def _spawn_with_pty(
    cmd: List[str],
) -> Optional[Tuple[subprocess.Popen, int]]:
    """Spawn *cmd* with stdout/stderr connected to a new pty.

    Returns (Popen, master_fd) on success, None on failure. Caller
    closes master_fd and terminates the subprocess.

    Why pty: dns-sd block-buffers its stdout when it's a pipe (default
    stdio behaviour for non-tty output). Resolutions are tiny (~100B)
    so they never reach us until the process exits — and dns-sd never
    exits on its own. A pty makes dns-sd think it's connected to a
    terminal, which switches stdio to line-buffered mode and our
    select() sees data immediately.
    """
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError as exc:
        logger.debug("dns-sd fallback: pty.openpty failed: %s", exc)
        return None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=slave_fd,
            stderr=slave_fd,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
    except (OSError, FileNotFoundError) as exc:
        logger.debug("dns-sd fallback: failed to spawn subprocess: %s", exc)
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            os.close(slave_fd)
        except OSError:
            pass
        return None
    # Parent doesn't need its end of the slave any more.
    try:
        os.close(slave_fd)
    except OSError:
        pass
    return proc, master_fd


def _read_dns_sd_output_pty(
    master_fd: int, timeout: float
) -> Tuple[Optional[str], Optional[int], Dict[bytes, bytes]]:
    """Pty version of :func:`_read_dns_sd_output`.

    Reads bytes from the pty master_fd and feeds them into a small
    line buffer. The parsing logic for ``can be reached at`` and
    ``key=value`` lines is identical to the pipe version.
    """
    deadline = time.monotonic() + timeout
    host: Optional[str] = None
    port: Optional[int] = None
    txt_props: Dict[bytes, bytes] = {}
    buf = bytearray()

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            ready, _, _ = select.select([master_fd], [], [], min(remaining, 0.25))
        except (ValueError, OSError):
            break
        if not ready:
            continue

        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)

        # Process each completed line.
        while b"\n" in buf or b"\r" in buf:
            for sep in (b"\r\n", b"\n", b"\r"):
                idx = buf.find(sep)
                if idx >= 0:
                    line_bytes = bytes(buf[:idx])
                    del buf[: idx + len(sep)]
                    break
            else:
                break
            try:
                line = line_bytes.decode("utf-8", errors="replace")
            except Exception:
                continue
            host, port = _maybe_parse_reachable(line, host, port)
            txt_props.update(_maybe_parse_txt(line))

        if host and port and txt_props:
            return host, port, txt_props

    return host, port, txt_props


def _maybe_parse_reachable(
    line: str, host: Optional[str], port: Optional[int]
) -> Tuple[Optional[str], Optional[int]]:
    if "can be reached at" not in line:
        return host, port
    tail = line.split("can be reached at", 1)[1].strip()
    host_port = tail.split()[0] if tail else ""
    if ":" not in host_port:
        return host, port
    h, p = host_port.rsplit(":", 1)
    try:
        return h, int(p)
    except ValueError:
        return host, port


def _maybe_parse_txt(line: str) -> Dict[bytes, bytes]:
    if "=" not in line:
        return {}
    out: Dict[bytes, bytes] = {}
    for token in line.strip().split():
        if "=" not in token:
            continue
        k, v = token.split("=", 1)
        if v.startswith('"') and v.endswith('"') and len(v) >= 2:
            v = v[1:-1]
        try:
            out[k.encode("utf-8")] = v.encode("utf-8")
        except UnicodeEncodeError:
            continue
    return out


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
