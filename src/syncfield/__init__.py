"""SyncField — lightweight timestamp capture for multi-stream synchronization.

Quick start::

    import syncfield as sf

    session = sf.SyncSession(host_id="rig_01", output_dir="./timestamps")
    session.start()

    frame = camera.read()
    session.stamp("cam_left", frame_number=0)

    session.stop()
"""

from syncfield.capture import SyncSession
from syncfield.types import FrameTimestamp, SyncPoint

__all__ = ["SyncSession", "SyncPoint", "FrameTimestamp"]
__version__ = "0.1.0"
