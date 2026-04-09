"""Top-level scan coordinator.

Two public functions live here:

- :func:`scan` — the primitive. Walks every registered discoverer in a
  thread pool, respects an overall time budget, and returns an immutable
  :class:`DiscoveryReport`. Never raises — partial failures become
  report ``errors`` entries.

- :func:`scan_and_add` — the convenience wrapper. Calls :func:`scan`, then
  auto-generates stream ids and calls ``session.add(...)`` for each
  discovered device whose warnings are empty. Returns the list of devices
  that were actually added.

Both functions also implement a small result cache so repeated scans
within a few seconds don't re-run expensive BLE scans.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Mapping, Optional, Sequence, Tuple

from syncfield.discovery._id_gen import make_stream_id
from syncfield.discovery.registry import iter_discoverers
from syncfield.discovery.types import DiscoveredDevice, DiscoveryReport

if TYPE_CHECKING:
    from syncfield.orchestrator import SessionOrchestrator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scan result cache
# ---------------------------------------------------------------------------

# Short-lived cache so two calls to ``scan()`` within ~5 seconds share one
# result. The key is the ``(kinds_tuple, timeout)`` pair; different filter
# combinations get independent cache entries.

_SCAN_CACHE_TTL_S = 5.0
_scan_cache: dict[Tuple[Tuple[str, ...], float], Tuple[DiscoveryReport, float]] = {}
_scan_cache_lock = threading.Lock()


def _cache_key(kinds: Optional[Sequence[str]], timeout: float) -> Tuple[Tuple[str, ...], float]:
    """Build a stable dict key for the scan result cache."""
    kinds_tuple = tuple(sorted(kinds)) if kinds else ()
    return (kinds_tuple, timeout)


def _cached_scan(key: Tuple[Tuple[str, ...], float]) -> Optional[DiscoveryReport]:
    with _scan_cache_lock:
        entry = _scan_cache.get(key)
        if entry is None:
            return None
        report, cached_at = entry
        if time.monotonic() - cached_at > _SCAN_CACHE_TTL_S:
            _scan_cache.pop(key, None)
            return None
        return report


def _store_scan_cache(key: Tuple[Tuple[str, ...], float], report: DiscoveryReport) -> None:
    with _scan_cache_lock:
        _scan_cache[key] = (report, time.monotonic())


def clear_scan_cache() -> None:
    """Drop all cached scan results. Test hook and the ``use_cache=False`` path."""
    with _scan_cache_lock:
        _scan_cache.clear()


# ---------------------------------------------------------------------------
# scan()
# ---------------------------------------------------------------------------


def scan(
    *,
    kinds: Optional[Sequence[str]] = None,
    timeout: float = 10.0,
    use_cache: bool = True,
) -> DiscoveryReport:
    """Enumerate every device the registered discoverers can see.

    Args:
        kinds: Optional filter on Stream kind — ``["video"]`` to skip BLE
            sensors when you only want cameras, for example. When omitted
            or ``None``, every registered discoverer runs.
        timeout: Wall-clock budget for the whole scan in seconds. Each
            adapter's ``discover()`` gets this full budget, but the
            overall call won't exceed it — slow adapters end up in
            :attr:`DiscoveryReport.timed_out` instead of blocking faster
            ones. Default ``10.0``.
        use_cache: If True, return a cached result from a recent scan
            with the same filter when one exists (5 s TTL). Pass ``False``
            for a hard refresh after plugging new hardware in.

    Returns:
        An immutable :class:`DiscoveryReport`. Never raises — exceptions
        from individual discoverers land in :attr:`DiscoveryReport.errors`.
    """
    key = _cache_key(kinds, timeout)
    if use_cache:
        cached = _cached_scan(key)
        if cached is not None:
            return cached

    discoverers = list(iter_discoverers())
    if kinds:
        kind_filter = set(kinds)
        discoverers = [
            cls
            for cls in discoverers
            if getattr(cls, "_discovery_kind", None) in kind_filter
        ]

    start = time.monotonic()
    all_devices: List[DiscoveredDevice] = []
    errors: dict[str, str] = {}
    timed_out: List[str] = []

    if not discoverers:
        report = DiscoveryReport(
            devices=(),
            errors={},
            duration_s=time.monotonic() - start,
            timed_out=(),
        )
        _store_scan_cache(key, report)
        return report

    # Thread pool fans discover() calls out in parallel. Each discoverer
    # is called with the remaining time budget so slow ones can still
    # honor the caller's overall timeout.
    deadline = start + timeout
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(discoverers), 8),
        thread_name_prefix="sf-discover",
    ) as executor:
        futures = {
            executor.submit(_safe_discover, cls, timeout): cls
            for cls in discoverers
        }

        pending = set(futures.keys())
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = concurrent.futures.wait(
                pending,
                timeout=remaining,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                cls = futures[future]
                adapter_type = _adapter_type_of(cls)
                try:
                    result = future.result()
                    all_devices.extend(result)
                except Exception as exc:  # pragma: no cover — defensive
                    errors[adapter_type] = f"{type(exc).__name__}: {exc}"

        # Any still-pending futures blew past the budget.
        for future in pending:
            cls = futures[future]
            timed_out.append(_adapter_type_of(cls))
            future.cancel()

    report = DiscoveryReport(
        devices=tuple(all_devices),
        errors=dict(errors),
        duration_s=time.monotonic() - start,
        timed_out=tuple(timed_out),
    )
    _store_scan_cache(key, report)
    return report


def _safe_discover(cls: type, timeout: float) -> List[DiscoveredDevice]:
    """Call ``cls.discover(timeout=timeout)`` with strict error containment.

    Discoverers are expected to return an empty list on failure rather
    than raise, but we wrap in a try/except anyway so a misbehaving
    discoverer can't take down the whole scan.
    """
    try:
        devices = cls.discover(timeout=timeout)
    except Exception as exc:
        logger.debug(
            "%s.discover() raised: %s: %s",
            cls.__name__,
            type(exc).__name__,
            exc,
        )
        raise
    if devices is None:
        return []
    return list(devices)


def _adapter_type_of(cls: type) -> str:
    """Extract a stable adapter_type string from a class (for error keys)."""
    # Adapters set ``_discovery_adapter_type`` as a class-level string so
    # the scanner doesn't have to instantiate them.
    return getattr(cls, "_discovery_adapter_type", cls.__name__)


# ---------------------------------------------------------------------------
# scan_and_add()
# ---------------------------------------------------------------------------


def scan_and_add(
    session: "SessionOrchestrator",
    *,
    kinds: Optional[Sequence[str]] = None,
    id_prefix: str = "",
    output_dir: Optional[Path] = None,
    skip_existing: bool = True,
    timeout: float = 10.0,
) -> List[DiscoveredDevice]:
    """Discover devices and register them with a session in one call.

    The typical three-line setup for a script::

        session = sf.SessionOrchestrator(host_id="rig_01", output_dir="./data")
        sf.discovery.scan_and_add(session)
        session.start()

    Each discovered device gets an auto-generated stream id based on its
    :attr:`DiscoveredDevice.display_name`. Devices with non-empty
    :attr:`DiscoveredDevice.warnings` are skipped with an INFO log — those
    require manual construction (e.g. a generic BLE IMU that needs a
    ``characteristic_uuid``). Devices marked ``in_use=True`` are also
    skipped.

    Args:
        session: The :class:`SessionOrchestrator` to add discovered
            streams to. Must be in the ``IDLE`` state — adding streams
            to a running session is a bug in the calling code.
        kinds: Optional kind filter, forwarded to :func:`scan`.
        id_prefix: Optional string prepended to every generated stream id.
            Useful for namespacing across multiple rigs.
        output_dir: Directory to use for streams that accept an
            ``output_dir`` kwarg. Defaults to ``session.output_dir``
            when omitted.
        skip_existing: If True (default), silently skip devices whose
            auto-generated id is already registered in the session. Set
            to False to raise instead — useful for catching bugs.
        timeout: Scan budget, forwarded to :func:`scan`.

    Returns:
        The list of :class:`DiscoveredDevice` instances that were
        successfully registered with the session, in registration order.
        Does not include devices that were skipped, timed out, or errored.

    Raises:
        RuntimeError: If the session is not in the ``IDLE`` state.
    """
    # Lazy import to avoid pulling the orchestrator module into scan() path
    from syncfield.types import SessionState

    if session.state is not SessionState.IDLE:
        raise RuntimeError(
            f"scan_and_add requires session in IDLE state; got {session.state.value}"
        )

    effective_output_dir = Path(output_dir) if output_dir is not None else session.output_dir

    report = scan(kinds=kinds, timeout=timeout)

    if report.errors:
        for adapter_type, message in report.errors.items():
            logger.info("discovery error from %s: %s", adapter_type, message)
    if report.timed_out:
        logger.info("discovery timed out: %s", ", ".join(report.timed_out))

    # Snapshot the currently-registered ids so make_stream_id avoids them.
    # Accessing ``_streams`` directly is intentional — the viewer/poller
    # already reads the same attribute, and the SDK has no public
    # iteration surface for registered streams yet.
    existing_ids = set(session._streams.keys())  # noqa: SLF001

    added: List[DiscoveredDevice] = []

    for device in report.devices:
        if device.warnings:
            logger.info(
                "skipping %s: %s",
                device.display_name,
                device.warnings[0],
            )
            continue
        if device.in_use:
            logger.info(
                "skipping %s: device appears to be in use by another process",
                device.display_name,
            )
            continue

        try:
            stream_id = make_stream_id(
                device.display_name,
                existing_ids,
                prefix=id_prefix,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "could not generate id for %s: %s", device.display_name, exc
            )
            continue

        # Build the caller kwargs. Every adapter needs an ``id``; only
        # video adapters accept ``output_dir``.
        construct_kwargs: dict[str, Any] = {"id": stream_id}
        if device.accepts_output_dir:
            construct_kwargs["output_dir"] = effective_output_dir

        try:
            stream = device.construct(**construct_kwargs)
        except Exception as exc:
            logger.warning(
                "failed to construct stream for %s: %s: %s",
                device.display_name,
                type(exc).__name__,
                exc,
            )
            continue

        try:
            session.add(stream)
        except ValueError as exc:
            if skip_existing:
                logger.info(
                    "skipping %s: %s",
                    device.display_name,
                    exc,
                )
                continue
            raise

        existing_ids.add(stream_id)
        added.append(device)
        logger.info(
            "  + %-40s %s",
            stream_id,
            device.adapter_type,
        )

    logger.info(
        "scan_and_add registered %d of %d discovered devices",
        len(added),
        len(report.devices),
    )
    return added
