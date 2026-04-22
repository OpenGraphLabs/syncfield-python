"""Registry of active Detectors for a session."""

from __future__ import annotations

from typing import Iterator, List

from syncfield.health.detector import Detector


class DetectorRegistry:
    def __init__(self) -> None:
        self._detectors: List[Detector] = []

    def register(self, detector: Detector) -> None:
        if any(d.name == detector.name for d in self._detectors):
            raise ValueError(f"Detector '{detector.name}' is already registered")
        self._detectors.append(detector)

    def unregister(self, name: str) -> None:
        self._detectors = [d for d in self._detectors if d.name != name]

    def __iter__(self) -> Iterator[Detector]:
        return iter(list(self._detectors))

    def __len__(self) -> int:
        return len(self._detectors)
