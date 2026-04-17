"""Reference :class:`~syncfield.stream.Stream` adapters shipped with syncfield.

The default ``pip install syncfield`` ships a fully-functional single-host
recording stack: ``UVCWebcamStream`` for USB / Continuity cameras and the
browser-based viewer. Heavier or platform-specific adapters live behind an
optional extra:

=========================  =====================================  ==================================
Adapter                    Requires                               Install
=========================  =====================================  ==================================
``JSONLFileStream``        —                                      ``syncfield``
``MetaQuestHandStream``    —                                      ``syncfield``
``PollingSensorStream``    —                                      ``syncfield``
``PushSensorStream``       —                                      ``syncfield``
``UVCWebcamStream``        ``av`` + ``numpy``                     ``syncfield``  *(default)*
``HostAudioStream``        ``sounddevice`` + ``numpy``            ``syncfield[audio]``
``BLEImuGenericStream``    ``bleak``                              ``syncfield[ble]``
``OgloTactileStream``      ``bleak``                              ``syncfield[ble]``
``MetaQuestCameraStream``  ``httpx``                              ``syncfield[camera]``
``Go3SStream``             ``bleak`` + ``aiohttp`` + ``httpx``    ``syncfield[camera]``
``OakCameraStream``        ``depthai`` + ``av`` + ``numpy``       ``syncfield[oak]``
=========================  =====================================  ==================================

Optional adapters are imported eagerly when their extra is installed so
:func:`syncfield.discovery.scan` enumerates them automatically. When the
extra is missing, referencing the adapter raises :class:`AttributeError`
(surfaced by ``from syncfield.adapters import …`` as :class:`ImportError`)
with the exact ``pip install`` line — never a silent disappearance.

``OakCameraStream`` is a deliberate exception: even when the ``oak`` extra
is installed it is *not* eagerly imported, because depthai installs
process-wide ``SIGSEGV`` / ``SIGABRT`` handlers at import time. Those
handlers recurse infinitely when an unrelated native library (bleak, av…)
crashes, so a BLE-only session would otherwise be killed by an unrelated
failure. Lazy loading keeps the depthai blast radius confined to sessions
that actually use it.
"""

from __future__ import annotations

import logging
from importlib import import_module

from syncfield.adapters.jsonl_file import JSONLFileStream
from syncfield.adapters.meta_quest import MetaQuestHandStream
from syncfield.adapters.polling_sensor import PollingSensorStream
from syncfield.adapters.push_sensor import PushSensorStream
from syncfield.adapters.uvc_webcam import UVCWebcamStream
from syncfield.discovery import register_discoverer

logger = logging.getLogger(__name__)


def _safe_register(cls) -> None:
    """Register an adapter with the discovery registry, ignoring TypeErrors.

    Adapters that lack ``discover()`` / ``_discovery_kind`` are silently
    skipped — not every stream type needs to be auto-discoverable.
    """
    try:
        register_discoverer(cls)
    except TypeError:
        pass


_safe_register(UVCWebcamStream)


__all__ = [
    "JSONLFileStream",
    "MetaQuestHandStream",
    "PollingSensorStream",
    "PushSensorStream",
    "UVCWebcamStream",
]


# ---------------------------------------------------------------------------
# Optional adapters.
#
# Each entry: (module path, qualname, extra name, dependency hint).
# When the extra is installed → eager import + register.
# When it is missing → record in ``_MISSING_HINTS`` so ``__getattr__`` can
# raise an AttributeError with a precise install instruction on first
# access (instead of the silent "name vanished" behaviour of the older
# ``try: …; except ImportError: pass`` pattern).
# ---------------------------------------------------------------------------
_OPTIONAL: tuple[tuple[str, str, str, str], ...] = (
    ("syncfield.adapters.host_audio",        "HostAudioStream",       "audio",  "sounddevice + numpy"),
    ("syncfield.adapters.ble_imu",           "BLEImuGenericStream",   "ble",    "bleak"),
    ("syncfield.adapters.ble_imu",           "BLEImuProfile",         "ble",    "bleak"),
    ("syncfield.adapters.ble_imu",           "ChannelSpec",           "ble",    "bleak"),
    ("syncfield.adapters.ble_imu",           "ConfigWrite",           "ble",    "bleak"),
    ("syncfield.adapters.oglo_tactile",      "OgloTactileStream",     "ble",    "bleak"),
    ("syncfield.adapters.meta_quest_camera", "MetaQuestCameraStream", "camera", "httpx"),
    ("syncfield.adapters.insta360_go3s",     "Go3SStream",            "camera", "bleak + aiohttp + httpx"),
)

_MISSING_HINTS: dict[str, tuple[str, str, str]] = {}

for _module_path, _qualname, _extra, _hint in _OPTIONAL:
    try:
        _module = import_module(_module_path)
        _obj = getattr(_module, _qualname)
    except ImportError as _e:
        _MISSING_HINTS[_qualname] = (_extra, _hint, str(_e))
        logger.debug(
            "%s unavailable (install syncfield[%s]): %s",
            _qualname, _extra, _e,
        )
        continue
    globals()[_qualname] = _obj
    if _qualname not in __all__:
        __all__.append(_qualname)
    if hasattr(_obj, "_discovery_kind"):
        _safe_register(_obj)


# ---------------------------------------------------------------------------
# OakCameraStream is *always* deferred via __getattr__, even when the
# 'oak' extra is installed. Depthai installs process-wide SIGSEGV/SIGABRT
# handlers at import time and those handlers recurse infinitely when any
# unrelated native library crashes — quarantining the import to the
# moment the user actually references OakCameraStream means a pure-BLE
# session never carries the depthai blast radius.
# ---------------------------------------------------------------------------
_OAK_LOADED = False


def _missing_extra_error(name: str, extra: str, deps: str, err: str) -> ImportError:
    """Build an ImportError that survives ``from syncfield.adapters import X``.

    We deliberately raise :class:`ImportError` (not :class:`AttributeError`)
    from :func:`__getattr__` because CPython's ``from A import B`` machinery
    catches AttributeError, drops its message, and re-raises a generic
    ``cannot import name 'B' from 'A'`` — losing the install hint. An
    ImportError is propagated verbatim, so the user sees the actionable
    message regardless of import form.
    """
    return ImportError(
        f"{name} requires the {extra!r} optional dependency ({deps}). "
        f"Install with `pip install 'syncfield[{extra}]'`. "
        f"Underlying error: {err}",
        name=name,
    )


def __getattr__(name: str):
    """PEP 562 hook — resolve OakCameraStream lazily and surface install hints."""
    global _OAK_LOADED
    if name == "OakCameraStream":
        try:
            from syncfield.adapters.oak_camera import OakCameraStream
        except ImportError as e:
            raise _missing_extra_error(
                "OakCameraStream", "oak", "depthai + av + numpy", str(e)
            ) from e
        if not _OAK_LOADED:
            _safe_register(OakCameraStream)
            if "OakCameraStream" not in __all__:
                __all__.append("OakCameraStream")
            _OAK_LOADED = True
        globals()["OakCameraStream"] = OakCameraStream
        return OakCameraStream

    hint = _MISSING_HINTS.get(name)
    if hint is not None:
        extra, deps, err = hint
        raise _missing_extra_error(name, extra, deps, err)
    raise AttributeError(name)


def __dir__() -> list[str]:
    """Surface lazy / missing-extra adapters to dir() and tab completion."""
    return sorted(set(__all__) | set(_MISSING_HINTS.keys()) | {"OakCameraStream"})
