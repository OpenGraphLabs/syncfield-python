"""Shared BLE peripheral scan with short-lived caching.

BLE scanning is *slow* (1-5 seconds depending on advertisement interval)
and every adapter that uses ``bleak`` wants to walk the same result set.
Without coordination, two BLE-based discoverers running in parallel
would each kick off an independent ``BleakScanner`` run — not only
slower, but on macOS it can actually cause one of them to hang or
return garbage because CoreBluetooth doesn't expect concurrent scanners.

This module exposes :func:`scan_peripherals`, a thread-safe coordinator
around ``BleakScanner.discover()``. Concurrent callers share one scan:
the first caller runs it while subsequent callers block on a lock,
then all callers see the same result. Results are also cached for a
short TTL so back-to-back ``scan()`` calls don't re-run Bluetooth.

Two caps matter:

- ``_CACHE_TTL_S`` — how long a fresh scan result is served to later
  callers without hitting Bluetooth again.
- ``_MAX_SCAN_S``  — hard ceiling on the BleakScanner window, regardless
  of what the caller requests. BLE advertisement cycles are 1-4
  seconds, so anything beyond ~5s is wasted time.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, List

logger = logging.getLogger(__name__)


# Cached scan results keyed by nothing (single global cache). Adapter-
# specific filtering happens after fetching from this cache — all BLE
# discoverers see the same raw peripheral list.
_cache: List[Any] = []
_cache_time: float = 0.0

# Single lock guards both the cache AND the in-flight scan. Held for the
# full duration of a ``BleakScanner.discover()`` call so two callers
# racing into the module serialize onto one scan result.
_scan_lock = threading.Lock()

# Short cache TTL — long enough to share one ``scan()`` round across
# adapters, short enough that back-to-back user-triggered rescans feel
# responsive.
_CACHE_TTL_S = 3.0

# Hard cap on the BleakScanner window. BLE ads repeat every 1-4 s on
# typical peripherals; scanning longer than this wastes wall-clock
# time for essentially zero extra coverage.
_MAX_SCAN_S = 5.0


def scan_peripherals(timeout: float = 5.0) -> List[Any]:
    """Return the list of BLE peripherals currently in range.

    Under the hood this runs ``bleak.BleakScanner.discover()`` on a
    throwaway asyncio loop and caches the result for a few seconds so
    subsequent callers skip the rescan. Concurrent callers serialize
    on a single shared scan — the first one runs it, the others block
    on the lock and then get the cached result.

    Args:
        timeout: Requested BLE scan window in seconds. Capped to
            :data:`_MAX_SCAN_S` internally; values larger than the cap
            are clamped silently. Ignored entirely on a cache hit.

    Returns:
        List of ``BLEDevice``-like objects (whatever ``bleak`` returns).
        Empty list on any failure — missing ``bleak`` install, platform
        Bluetooth adapter error, etc. Discovery is never allowed to raise
        into the scan coordinator.
    """
    global _cache, _cache_time

    effective_timeout = min(max(0.5, timeout), _MAX_SCAN_S)

    # Single lock held across the whole call: cache check, scan, cache
    # update. This serializes concurrent callers onto one shared result
    # instead of letting them run parallel BleakScanner instances —
    # which macOS CoreBluetooth doesn't handle well.
    with _scan_lock:
        now = time.monotonic()
        if _cache and (now - _cache_time) < _CACHE_TTL_S:
            return list(_cache)

        try:
            import bleak  # type: ignore[import-not-found]
        except ImportError:
            logger.debug("bleak not available; BLE discovery returns empty list")
            return []

        try:
            loop = asyncio.new_event_loop()
            try:
                devices = loop.run_until_complete(
                    bleak.BleakScanner.discover(timeout=effective_timeout)
                )
            finally:
                loop.close()
        except Exception as exc:
            logger.debug("BLE scan failed: %s", exc)
            return []

        _cache = list(devices)
        _cache_time = time.monotonic()
        return list(devices)


def clear_cache() -> None:
    """Invalidate the shared BLE scan cache. Primarily a test hook."""
    global _cache, _cache_time
    with _cache_lock:
        _cache = []
        _cache_time = 0.0
