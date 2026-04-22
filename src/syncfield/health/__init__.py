"""syncfield.health — platform health telemetry."""

from __future__ import annotations

import sys as _sys

# Guard flag — checked via __dict__ to avoid re-entering __getattr__.
_health_imported: bool = False


def __getattr__(name: str) -> object:
    # Lazy-load to avoid a circular import:
    #   syncfield.types → syncfield.health.severity
    #   → syncfield.health (this __init__) → syncfield.health.detector
    #   → syncfield.health.types → syncfield.types (partially initialised)
    _import_all()
    try:
        return _sys.modules[__name__].__dict__[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def _import_all() -> None:
    """Populate the module namespace on first access."""
    mod = _sys.modules[__name__]
    # Use __dict__ directly to avoid triggering __getattr__ again.
    if mod.__dict__.get("_health_imported"):
        return
    mod.__dict__["_health_imported"] = True

    from syncfield.health.detector import Detector, DetectorBase
    from syncfield.health.registry import DetectorRegistry
    from syncfield.health.severity import Severity, max_severity
    from syncfield.health.system import HealthSystem
    from syncfield.health.tracker import IncidentTracker
    from syncfield.health.types import (
        Incident,
        IncidentArtifact,
        IncidentSnapshot,
        WriterStats,
    )

    ns = mod.__dict__
    ns["Detector"] = Detector
    ns["DetectorBase"] = DetectorBase
    ns["DetectorRegistry"] = DetectorRegistry
    ns["Severity"] = Severity
    ns["max_severity"] = max_severity
    ns["HealthSystem"] = HealthSystem
    ns["IncidentTracker"] = IncidentTracker
    ns["Incident"] = Incident
    ns["IncidentArtifact"] = IncidentArtifact
    ns["IncidentSnapshot"] = IncidentSnapshot
    ns["WriterStats"] = WriterStats


__all__ = [
    "Detector",
    "DetectorBase",
    "DetectorRegistry",
    "HealthSystem",
    "Incident",
    "IncidentArtifact",
    "IncidentSnapshot",
    "IncidentTracker",
    "Severity",
    "WriterStats",
    "max_severity",
]
