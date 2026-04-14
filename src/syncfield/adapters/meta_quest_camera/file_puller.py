"""Pulls the four per-session artifacts (2 MP4s + 2 timestamps JSONLs) from
the Quest's HTTP surface into the SyncField session output directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from syncfield.adapters.meta_quest_camera.http_client import QuestHttpClient


@dataclass(frozen=True)
class RecordingArtifacts:
    """Paths written to ``output_dir`` by a successful ``pull_all``."""

    left_mp4: Path
    right_mp4: Path
    left_timestamps: Path
    right_timestamps: Path


class RecordingFilePuller:
    """Downloads all per-session artifacts into ``output_dir``.

    File naming mirrors the adapter's public contract:

    - ``{stream_id}_{side}.mp4``
    - ``{stream_id}_{side}.timestamps.jsonl``
    """

    def __init__(
        self,
        *,
        client: QuestHttpClient,
        stream_id: str,
        output_dir: Path,
    ) -> None:
        self._client = client
        self._stream_id = stream_id
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def pull_all(self) -> RecordingArtifacts:
        prefix = self._stream_id
        paths = RecordingArtifacts(
            left_mp4=self._output_dir / f"{prefix}_left.mp4",
            right_mp4=self._output_dir / f"{prefix}_right.mp4",
            left_timestamps=self._output_dir / f"{prefix}_left.timestamps.jsonl",
            right_timestamps=self._output_dir / f"{prefix}_right.timestamps.jsonl",
        )
        self._client.download_file("/recording/files/left", paths.left_mp4)
        self._client.download_file("/recording/files/right", paths.right_mp4)
        self._client.download_file(
            "/recording/timestamps/left", paths.left_timestamps
        )
        self._client.download_file(
            "/recording/timestamps/right", paths.right_timestamps
        )
        return paths
