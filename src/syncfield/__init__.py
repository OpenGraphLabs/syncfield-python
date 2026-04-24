"""SyncField — capture orchestration framework for multi-modal synchronization.

Quick start::

    import syncfield as sf
    from syncfield.adapters import UVCWebcamStream, JSONLFileStream

    session = sf.SessionOrchestrator(
        host_id="rig_01",
        output_dir="./data",
    )
    session.add(UVCWebcamStream("cam_main", device_index=0, output_dir="./data"))
    session.add(JSONLFileStream("sensor_log", file_path="./data/sensor.jsonl"))

    session.start()
    # ... recording ...
    report = session.stop()

See the :mod:`syncfield.adapters` subpackage for built-in reference adapters
and :mod:`syncfield.testing` for utilities like :class:`~syncfield.testing.FakeStream`
used in unit tests.
"""

import logging as _logging
import os as _os
from importlib.metadata import version as _pkg_version

# Default the ``syncfield`` logger to INFO with a concise stream handler
# so adapter lifecycle and fan-out timing surface in the console without
# callers having to configure logging themselves. Honours any prior
# configuration: if the app already set a level or attached handlers to
# this logger, we leave it alone. Callers can also force-silence via
# ``SYNCFIELD_LOG_LEVEL=WARNING`` (or any standard level name).
def _configure_default_logging() -> None:
    logger = _logging.getLogger("syncfield")
    env_level = _os.environ.get("SYNCFIELD_LOG_LEVEL")
    if env_level:
        level = _logging.getLevelName(env_level.upper())
        if isinstance(level, int):
            logger.setLevel(level)
    elif logger.level == _logging.NOTSET:
        logger.setLevel(_logging.INFO)
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(
            _logging.Formatter("%(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        # Don't double-emit through the root logger if an app configures
        # basicConfig later — our handler is sufficient.
        logger.propagate = False


_configure_default_logging()

from syncfield.clock import SessionClock
from syncfield.orchestrator import SessionOrchestrator
from syncfield.roles import FollowerRole, LeaderRole, RoleKind
from syncfield.stream import Stream, StreamBase
from syncfield.tone import ChirpSpec, SyncToneConfig
from syncfield.types import (
    ChirpEmission,
    ChirpSource,
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    SessionReport,
    SessionState,
    StreamCapabilities,
    StreamKind,
    SyncPoint,
)

__all__ = [
    # Core orchestrator
    "SessionOrchestrator",
    "SessionClock",
    "SessionState",
    "SessionReport",
    "FinalizationReport",
    # Stream SPI + capabilities
    "Stream",
    "StreamBase",
    "StreamCapabilities",
    "StreamKind",
    "SampleEvent",
    "HealthEvent",
    "HealthEventKind",
    "SyncPoint",
    # Sync tone / chirp
    "SyncToneConfig",
    "ChirpSpec",
    "ChirpEmission",
    "ChirpSource",
    # Multi-host roles (opt-in)
    "LeaderRole",
    "FollowerRole",
    "RoleKind",
]
__version__ = _pkg_version("syncfield")
