"""Global registry of adapter classes that support discovery.

Each Stream adapter that implements a ``@classmethod discover(cls, *,
timeout)`` registers itself here at import time. :func:`scan` walks the
registry, filters by ``kinds=``, and fans out to each ``discover()`` call
in a thread pool.

Third-party adapter authors register their classes the same way the
shipped adapters do::

    from syncfield.discovery import register_discoverer

    class MyBrandIMU(StreamBase):
        _discovery_kind = "sensor"

        @classmethod
        def discover(cls, *, timeout: float = 5.0):
            return [...]

    register_discoverer(MyBrandIMU)

The registry is module-global and thread-safe for registration. The only
mutation is list append during import, so a lock is conservative but
cheap.
"""

from __future__ import annotations

import logging
import threading
from typing import List, Type

logger = logging.getLogger(__name__)


# Module-level state — small and finite. The list holds adapter classes
# (not instances); discovery calls class methods on each one.
_REGISTERED: List[Type] = []
_LOCK = threading.Lock()


def register_discoverer(adapter_cls: Type) -> None:
    """Register an adapter class with the discovery system.

    The class must implement:

    - ``discover(cls, *, timeout: float) -> list[DiscoveredDevice]`` as a
      classmethod — enumerates the currently attached devices.
    - ``_discovery_kind`` as a class-level string — ``"video" | "sensor" |
      "audio" | "custom"``. Used by ``scan(kinds=...)`` filtering so
      :func:`scan` can skip whole adapter classes without calling their
      discover() methods (important when BLE scanning is expensive).

    Silent no-op if the class is already registered — idempotent so
    re-importing the adapters package during test reloads is safe.

    Raises:
        TypeError: If the class is missing ``discover()`` or
            ``_discovery_kind``. Failing loud here is better than a silent
            runtime surprise later.
    """
    if not hasattr(adapter_cls, "discover"):
        raise TypeError(
            f"{adapter_cls.__name__} cannot be registered as a discoverer: "
            f"missing 'discover' classmethod"
        )
    if not hasattr(adapter_cls, "_discovery_kind"):
        raise TypeError(
            f"{adapter_cls.__name__} cannot be registered as a discoverer: "
            f"missing '_discovery_kind' class attribute "
            f"(one of 'video', 'audio', 'sensor', 'custom')"
        )

    with _LOCK:
        if adapter_cls in _REGISTERED:
            return
        _REGISTERED.append(adapter_cls)
        logger.debug(
            "registered discoverer: %s (kind=%s)",
            adapter_cls.__name__,
            getattr(adapter_cls, "_discovery_kind", "?"),
        )


def unregister_discoverer(adapter_cls: Type) -> bool:
    """Remove a class from the registry. Returns True if it was present.

    Primarily useful for tests that want a clean registry state.
    """
    with _LOCK:
        try:
            _REGISTERED.remove(adapter_cls)
            return True
        except ValueError:
            return False


def iter_discoverers() -> tuple:
    """Return a snapshot tuple of currently registered adapter classes.

    Snapshot avoids iteration-during-mutation if a third-party module
    registers a new discoverer mid-scan — the current :func:`scan`
    never sees the new class until the next call.
    """
    with _LOCK:
        return tuple(_REGISTERED)


def clear_registry() -> None:
    """Drop every registered discoverer. Intended for test isolation."""
    with _LOCK:
        _REGISTERED.clear()
