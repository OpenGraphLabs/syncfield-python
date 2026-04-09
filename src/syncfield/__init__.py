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

from importlib.metadata import version as _pkg_version

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
