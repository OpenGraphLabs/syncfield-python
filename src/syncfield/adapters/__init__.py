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
"""

from syncfield.adapters.jsonl_file import JSONLFileStream

__all__ = ["JSONLFileStream"]


# ---------------------------------------------------------------------------
# Optional re-exports — never fatal if the corresponding extra is missing.
# ---------------------------------------------------------------------------

try:
    from syncfield.adapters.uvc_webcam import UVCWebcamStream  # noqa: F401
    __all__.append("UVCWebcamStream")
except ImportError:
    pass

try:
    from syncfield.adapters.ble_imu import BLEImuGenericStream  # noqa: F401
    __all__.append("BLEImuGenericStream")
except ImportError:
    pass

try:
    from syncfield.adapters.oglo_tactile import OgloTactileStream  # noqa: F401
    __all__.append("OgloTactileStream")
except ImportError:
    pass

try:
    from syncfield.adapters.oak_camera import OakCameraStream  # noqa: F401
    __all__.append("OakCameraStream")
except ImportError:
    pass
