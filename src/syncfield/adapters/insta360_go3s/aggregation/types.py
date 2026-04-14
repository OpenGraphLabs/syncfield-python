"""Data types for the Insta360 Go3S aggregation queue."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class AggregationState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AggregationCameraSpec:
    stream_id: str
    ble_address: str
    wifi_ssid: str
    wifi_password: str
    sd_path: str
    local_filename: str
    size_bytes: int
    done: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AggregationCameraSpec":
        return cls(**data)


@dataclass
class AggregationJob:
    job_id: str
    episode_id: str
    episode_dir: Path
    cameras: list[AggregationCameraSpec]
    state: AggregationState = AggregationState.PENDING
    started_at_ns: Optional[int] = None
    completed_at_ns: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "episode_id": self.episode_id,
            "episode_dir": str(self.episode_dir),
            "cameras": [c.to_dict() for c in self.cameras],
            "state": self.state.value,
            "started_at_ns": self.started_at_ns,
            "completed_at_ns": self.completed_at_ns,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AggregationJob":
        return cls(
            job_id=data["job_id"],
            episode_id=data["episode_id"],
            episode_dir=Path(data["episode_dir"]),
            cameras=[AggregationCameraSpec.from_dict(c) for c in data["cameras"]],
            state=AggregationState(data["state"]),
            started_at_ns=data.get("started_at_ns"),
            completed_at_ns=data.get("completed_at_ns"),
            error=data.get("error"),
        )

    def manifest_path(self) -> Path:
        return self.episode_dir / "aggregation.json"

    def write_manifest(self) -> None:
        self.manifest_path().parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path().write_text(json.dumps(self.to_dict(), indent=2))


@dataclass
class AggregationProgress:
    """Point-in-time aggregation status broadcast to subscribers.

    ``stage`` is a short machine-readable token describing the current
    step (``"switching_wifi"``, ``"waiting_for_ap"``, ``"probing"``,
    ``"downloading"``, ``"restoring_wifi"``). It stays in sync with the
    downloader's phase so the UI can show meaningful feedback even
    before the first download byte arrives — WiFi switch + DHCP wait can
    take 10–30 s before any chunk callback fires, and without a stage
    hint the bar would sit at "0% · 0 B / 0 B" looking stuck.
    """

    job_id: str
    episode_id: str
    state: AggregationState
    cameras_total: int
    cameras_done: int
    current_stream_id: Optional[str] = None
    current_bytes: int = 0
    current_total_bytes: int = 0
    stage: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "episode_id": self.episode_id,
            "state": self.state.value,
            "cameras_total": self.cameras_total,
            "cameras_done": self.cameras_done,
            "current_stream_id": self.current_stream_id,
            "current_bytes": self.current_bytes,
            "current_total_bytes": self.current_total_bytes,
            "stage": self.stage,
            "error": self.error,
        }
