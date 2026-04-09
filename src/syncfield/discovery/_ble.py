"""Shared BLE peripheral scan with short-lived caching.

BLE scanning is *slow* (5-10 seconds depending on the OS) and every
adapter that uses ``bleak`` wants to walk the same result set. Without
coordination, two BLE-based discoverers running in parallel would each
kick off an independent scan and the user would wait 10+ seconds instead
of 5.

This module exposes :func:`scan_peripherals`, a thread-safe cache around
``BleakScanner.discover()``. The first caller runs the scan; everyone
else within ``_CACHE_TTL_S`` gets the cached result back immediately.

The cache is *very* short-lived by design (3 seconds) — BLE devices
come and go frequently, and discovery is expected to surface the current
state, not a stale snapshot.
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
_cache_lock = threading.Lock()

# Short TTL. Long enough to share one ``scan()`` round across adapters,
# short enough that back-to-back user-triggered rescans feel responsive.
_CACHE_TTL_S = 3.0


def scan_peripherals(timeout: float = 5.0) -> List[Any]:
    """Return the list of BLE peripherals currently in range.

    Under the hood this runs ``bleak.BleakScanner.discover()`` on a
    throwaway asyncio loop and caches the result for a few seconds so
    subsequent callers skip the rescan.

    Args:
        timeout: BLE scan window in seconds. Ignored on cache hit.

    Returns:
        List of ``BLEDevice``-like objects (whatever ``bleak`` returns).
        Empty list on any failure — missing ``bleak`` install, platform
        Bluetooth adapter error, etc. Discovery is never allowed to raise
        into the scan coordinator.
    """
    global _cache, _cache_time

    # Cache hit path — fast, no subprocess or asyncio overhead.
    with _cache_lock:
        now = time.monotonic()
        if _cache and (now - _cache_time) < _CACHE_TTL_S:
            return list(_cache)

    # Cache miss: import bleak lazily so the module stays importable on
    # machines without the BLE extra installed.
    try:
        import bleak  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("bleak not available; BLE discovery returns empty list")
        return []

    try:
        loop = asyncio.new_event_loop()
        try:
            devices = loop.run_until_complete(
                bleak.BleakScanner.discover(timeout=timeout)
            )
        finally:
            loop.close()
    except Exception as exc:
        logger.debug("BLE scan failed: %s", exc)
        return []

    # Update the cache under lock; the list is intentionally a fresh copy
    # so a reader that mutates its own copy can't affect the cache.
    with _cache_lock:
        _cache = list(devices)
        _cache_time = time.monotonic()
    return list(devices)


def clear_cache() -> None:
    """Invalidate the shared BLE scan cache. Primarily a test hook."""
    global _cache, _cache_time
    with _cache_lock:
        _cache = []
        _cache_time = 0.0
