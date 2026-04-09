"""Reference :class:`~syncfield.stream.Stream` adapters shipped with syncfield.

Adapters with no external dependencies are always re-exported here.
Adapters gated behind optional extras are re-exported **lazily** — if the
corresponding extra is not installed, importing ``syncfield.adapters`` still
succeeds but that specific class is simply absent from the module.

=========================  =====================================  =============================
Adapter                    Requires                               Install
=========================  =====================================  =============================
``JSONLFileStream``        —                                      ``syncfield``
``UVCWebcamStream``        ``opencv-python``                      ``syncfield[uvc]``
``BLEImuGenericStream``    ``bleak``                              ``syncfield[ble]``
``OgloTactileStream``      ``bleak``                              ``syncfield[ble]``
``OakCameraStream``        ``depthai`` + ``opencv-python``        ``syncfield[oak,uvc]``
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
from syncfield.discovery import register_discoverer

__all__ = ["JSONLFileStream"]


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
    from syncfield.adapters.ble_imu import BLEImuGenericStream  # noqa: F401
    __all__.append("BLEImuGenericStream")
    _safe_register(BLEImuGenericStream)
except ImportError:
    pass

try:
    from syncfield.adapters.oglo_tactile import OgloTactileStream  # noqa: F401
    __all__.append("OgloTactileStream")
    _safe_register(OgloTactileStream)
except ImportError:
    pass

try:
    from syncfield.adapters.oak_camera import OakCameraStream  # noqa: F401
    __all__.append("OakCameraStream")
    _safe_register(OakCameraStream)
except ImportError:
    pass
