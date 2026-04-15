"""Reference :class:`~syncfield.stream.Stream` adapters shipped with syncfield.

Adapters with no external dependencies are always re-exported here.
Adapters gated behind optional extras are re-exported **lazily** — if the
corresponding extra is not installed, importing ``syncfield.adapters`` still
succeeds but that specific class is simply absent from the module.

=========================  =====================================  =============================
Adapter                    Requires                               Install
=========================  =====================================  =============================
``JSONLFileStream``        —                                      ``syncfield``
``UVCWebcamStream``        ``av``                                 ``syncfield[uvc]``
``BLEImuGenericStream``    ``bleak``                              ``syncfield[ble]``
``OgloTactileStream``      ``bleak``                              ``syncfield[ble]``
``OakCameraStream``        ``depthai`` + ``av``                   ``syncfield[oak]``
``Go3SStream``             ``bleak`` + ``aiohttp``                ``syncfield[camera]``
=========================  =====================================  =============================

Users who need a specific optional adapter can always import it directly
(e.g. ``from syncfield.adapters.uvc_webcam import UVCWebcamStream``) — that
path raises a clear :class:`ImportError` with an install hint when the
dependency is missing.

Adapters that implement a ``discover()`` classmethod are **automatically
registered with the discovery registry** here at import time, so
``syncfield.discovery.scan()`` walks them without any explicit plumbing
from the caller.
"""

from syncfield.adapters.jsonl_file import JSONLFileStream
from syncfield.adapters.meta_quest import MetaQuestHandStream
from syncfield.adapters.meta_quest_camera import MetaQuestCameraStream
from syncfield.adapters.polling_sensor import PollingSensorStream
from syncfield.adapters.push_sensor import PushSensorStream
from syncfield.discovery import register_discoverer

__all__ = [
    "JSONLFileStream",
    "MetaQuestCameraStream",
    "MetaQuestHandStream",
    "PollingSensorStream",
    "PushSensorStream",
]

# HostAudioStream requires the 'audio' extra (sounddevice + numpy).
try:
    from syncfield.adapters.host_audio import HostAudioStream  # noqa: F401
    __all__.append("HostAudioStream")
except ImportError:
    pass


def _safe_register(cls) -> None:
    """Register an adapter with the discovery registry, swallowing errors.

    Keeping this defensive means a bad ``_discovery_kind`` / ``discover()``
    on one adapter never breaks the whole :mod:`syncfield.adapters` import.
    """
    try:
        register_discoverer(cls)
    except TypeError:
        # Adapter is missing the discover() classmethod or the
        # _discovery_kind attribute. Log-friendly fail: don't raise,
        # just don't register.
        pass


# ---------------------------------------------------------------------------
# Optional re-exports — never fatal if the corresponding extra is missing.
# Each adapter that imports cleanly is also registered with the discovery
# registry so ``syncfield.discovery.scan()`` enumerates it automatically.
# ---------------------------------------------------------------------------

try:
    from syncfield.adapters.uvc_webcam import UVCWebcamStream  # noqa: F401
    __all__.append("UVCWebcamStream")
    _safe_register(UVCWebcamStream)
except ImportError:
    pass

try:
    from syncfield.adapters.ble_imu import (  # noqa: F401
        BLEImuGenericStream,
        BLEImuProfile,
        ChannelSpec,
        ConfigWrite,
    )
    __all__ += [
        "BLEImuGenericStream",
        "BLEImuProfile",
        "ChannelSpec",
        "ConfigWrite",
    ]
    _safe_register(BLEImuGenericStream)
except ImportError:
    pass

try:
    from syncfield.adapters.oglo_tactile import OgloTactileStream  # noqa: F401
    __all__.append("OgloTactileStream")
    _safe_register(OgloTactileStream)
except ImportError:
    pass

# OakCameraStream is deferred to truly-lazy import via PEP-562 __getattr__
# below. Depthai installs process-wide signal handlers at import time that
# intercept SIGSEGV/SIGABRT from *any* library — if bleak or subprocess
# then triggers a native crash, depthai's handler recurses infinitely
# (you'll see frames 0..31 of backward::SignalHandling::sig_handler in
# the crash dump). Eager-importing depthai when the user doesn't use an
# OAK camera means their BLE-only session can be taken down by an
# unrelated third-party crash. Lazy-loading contains the damage:
# depthai only loads when someone actually references OakCameraStream.
_OAK_LOADED = False


def __getattr__(name: str):
    global _OAK_LOADED
    if name == "OakCameraStream":
        try:
            from syncfield.adapters.oak_camera import OakCameraStream
        except ImportError as e:
            raise AttributeError(
                f"OakCameraStream requires the 'oak' optional dependency "
                f"(depthai + av). Install with `uv add 'syncfield[oak]'`. "
                f"Underlying error: {e}"
            ) from e
        if not _OAK_LOADED:
            _safe_register(OakCameraStream)
            if "OakCameraStream" not in __all__:
                __all__.append("OakCameraStream")
            _OAK_LOADED = True
        return OakCameraStream
    raise AttributeError(name)

try:
    from syncfield.adapters.insta360_go3s import Go3SStream  # noqa: F401
    __all__.append("Go3SStream")
    _safe_register(Go3SStream)
except ImportError:
    pass
