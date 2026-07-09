"""Adapters must follow the orchestrator's episode dir, whatever they were given.

Real adapters (OakCameraStream, OgloTactileStream) cache an output directory at
construction and self-write files the orchestrator never sees. If the caller
passes the data root instead of ``session.output_dir``, those files land outside
the episode. This locks the guarantee that ``start()`` rebinds them first.
"""
from pathlib import Path

from syncfield.orchestrator import SessionOrchestrator
from syncfield.testing import FakeStream
from syncfield.tone import SyncToneConfig


class _DirSpyStream(FakeStream):
    """FakeStream that caches an output dir like a real adapter and records it."""

    def __init__(self, id: str, output_dir: Path) -> None:
        super().__init__(id)
        self._output_dir = Path(output_dir)
        self.seen_dirs: list[Path] = []

    def start_recording(self, session_clock) -> None:  # noqa: ANN001
        self.seen_dirs.append(self._output_dir)
        super().start_recording(session_clock)


def test_adapter_follows_episode_dir_even_when_constructed_with_data_root(tmp_path):
    session = SessionOrchestrator(
        host_id="t",
        output_dir=tmp_path,
        sync_tone=SyncToneConfig.silent(),
        enable_host_audio=False,
    )
    ep1 = session.output_dir
    assert ep1.parent == tmp_path
    assert ep1.name.startswith("ep_")

    # Deliberately hand the adapter the DATA ROOT, not session.output_dir.
    stream = _DirSpyStream("spy", tmp_path)
    session.add(stream)

    session.connect()
    session.start(countdown_s=0.0)
    session.stop()
    ep2 = session.output_dir
    assert ep2 != ep1

    session.start(countdown_s=0.0)
    session.stop()
    session.disconnect()

    assert stream.seen_dirs == [ep1, ep2]


def test_adapter_given_session_output_dir_is_unaffected(tmp_path):
    session = SessionOrchestrator(
        host_id="t",
        output_dir=tmp_path,
        sync_tone=SyncToneConfig.silent(),
        enable_host_audio=False,
    )
    ep1 = session.output_dir
    stream = _DirSpyStream("spy", ep1)
    session.add(stream)

    session.connect()
    session.start(countdown_s=0.0)
    session.stop()
    session.disconnect()

    assert stream.seen_dirs == [ep1]
