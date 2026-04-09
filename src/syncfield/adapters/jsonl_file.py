"""JSONLFileStream — adapt a customer-owned JSONL file into a Stream.

Use this adapter when you already have a process writing per-sample JSONL
records and you just want the orchestrator to track lifecycle and include
the file in the manifest. The adapter performs **no I/O of its own** during
recording — it only inspects the file on ``stop()`` to report a frame count.

This is the "bring your own writer" degenerate adapter; it ships with no
optional dependencies and is always importable.
"""

from __future__ import annotations

from pathlib import Path

from syncfield.clock import SessionClock
from syncfield.stream import StreamBase
from syncfield.types import FinalizationReport, StreamCapabilities


class JSONLFileStream(StreamBase):
    """Wraps an external JSONL file as a Stream.

    The caller is responsible for writing the file on their own schedule —
    this adapter only tracks lifecycle and reports the file path in the
    :class:`FinalizationReport` (and thus the manifest). On ``stop()`` it
    counts the number of lines in the file; if the file does not exist,
    the status is ``"partial"``.

    Args:
        id: Stream id.
        file_path: Path to the JSONL file that the customer will write.
    """

    def __init__(self, id: str, file_path: Path | str) -> None:
        super().__init__(
            id=id,
            kind="sensor",
            capabilities=StreamCapabilities(
                provides_audio_track=False,
                # Precision depends on the customer's writer, not this adapter,
                # so we conservatively advertise False.
                supports_precise_timestamps=False,
                is_removable=False,
                produces_file=True,
            ),
        )
        self._file_path = Path(file_path)
        self._prepared = False
        self._started = False

    def prepare(self) -> None:
        self._prepared = True

    def start(self, session_clock: SessionClock) -> None:
        if not self._prepared:
            raise RuntimeError("JSONLFileStream.start() called without prepare()")
        self._started = True

    def stop(self) -> FinalizationReport:
        frame_count = 0
        status: str = "completed"
        file_path = self._file_path

        if file_path.exists():
            with file_path.open() as f:
                frame_count = sum(1 for _ in f)
        else:
            status = "partial"
            file_path = None  # type: ignore[assignment]

        return FinalizationReport(
            stream_id=self.id,
            status=status,  # type: ignore[arg-type]
            frame_count=frame_count,
            file_path=file_path,
            first_sample_at_ns=None,
            last_sample_at_ns=None,
            health_events=list(self._collected_health),
            error=None,
        )
