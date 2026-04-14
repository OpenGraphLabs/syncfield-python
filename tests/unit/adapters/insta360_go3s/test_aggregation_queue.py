import asyncio
from pathlib import Path
from typing import Any

import pytest

from syncfield.adapters.insta360_go3s.aggregation.queue import (
    AggregationDownloader,
    AggregationQueue,
)
from syncfield.adapters.insta360_go3s.aggregation.types import (
    AggregationCameraSpec,
    AggregationJob,
    AggregationProgress,
    AggregationState,
)


class FakeDownloader(AggregationDownloader):
    """Test double that simulates WiFi switch + OSC download."""

    def __init__(self, *, fail_on: set[str] | None = None):
        self.fail_on = fail_on or set()
        self.actions: list[str] = []

    async def run(self, camera: AggregationCameraSpec, target_dir: Path,
                  on_chunk: Any) -> None:
        self.actions.append(f"download:{camera.stream_id}")
        if camera.stream_id in self.fail_on:
            raise RuntimeError(f"injected failure for {camera.stream_id}")
        # Simulate two progress chunks then a completed file
        on_chunk(camera.stream_id, 6, camera.size_bytes)
        on_chunk(camera.stream_id, 12, camera.size_bytes)
        target = target_dir / camera.local_filename
        target.write_bytes(b"x" * 12)


def _make_job(tmp_path: Path, *stream_ids: str, size: int = 12) -> AggregationJob:
    return AggregationJob(
        job_id=f"job_{'_'.join(stream_ids)}",
        episode_id="ep_x",
        episode_dir=tmp_path,
        cameras=[
            AggregationCameraSpec(
                stream_id=sid,
                ble_address=f"AA:{sid}",
                wifi_ssid=f"Go3S-{sid}.OSC",
                wifi_password="88888888",
                sd_path=f"/DCIM/Camera01/{sid}.mp4",
                local_filename=f"{sid}.mp4",
                size_bytes=size,
            )
            for sid in stream_ids
        ],
    )


@pytest.mark.asyncio
async def test_enqueue_runs_to_completion(tmp_path):
    downloader = FakeDownloader()
    progress_log: list[AggregationProgress] = []
    q = AggregationQueue(downloader=downloader)
    q.subscribe(lambda p: progress_log.append(p))
    await q.start()

    job = _make_job(tmp_path, "cam_a", "cam_b")
    handle = q.enqueue(job)
    final = await handle.wait()

    assert final.state == AggregationState.COMPLETED
    assert final.cameras_done == 2
    assert (tmp_path / "cam_a.mp4").exists()
    assert (tmp_path / "cam_b.mp4").exists()
    assert any(p.state == AggregationState.RUNNING for p in progress_log)
    assert progress_log[-1].state == AggregationState.COMPLETED
    await q.shutdown()


@pytest.mark.asyncio
async def test_failure_marks_job_failed_and_preserves_other_files(tmp_path):
    downloader = FakeDownloader(fail_on={"cam_b"})
    q = AggregationQueue(downloader=downloader)
    await q.start()
    job = _make_job(tmp_path, "cam_a", "cam_b")
    handle = q.enqueue(job)
    final = await handle.wait()
    assert final.state == AggregationState.FAILED
    assert "cam_b" in (final.error or "")
    # cam_a should have completed
    assert (tmp_path / "cam_a.mp4").exists()
    await q.shutdown()


@pytest.mark.asyncio
async def test_retry_re_runs_only_failed_cameras(tmp_path):
    downloader = FakeDownloader(fail_on={"cam_b"})
    q = AggregationQueue(downloader=downloader)
    await q.start()
    job = _make_job(tmp_path, "cam_a", "cam_b")
    handle = q.enqueue(job)
    await handle.wait()

    # Heal the downloader and retry
    downloader.fail_on = set()
    downloader.actions.clear()
    handle2 = q.retry(job.job_id)
    final = await handle2.wait()
    assert final.state == AggregationState.COMPLETED
    # Only cam_b should have been re-downloaded
    assert downloader.actions == ["download:cam_b"]
    await q.shutdown()


@pytest.mark.asyncio
async def test_recover_pending_jobs_from_disk(tmp_path):
    job = _make_job(tmp_path, "cam_a")
    job.write_manifest()

    downloader = FakeDownloader()
    q = AggregationQueue(downloader=downloader)
    recovered = q.recover_from_disk(search_root=tmp_path.parent)
    assert any(j.job_id == job.job_id for j in recovered)
    await q.start()
    handle = q.enqueue(recovered[0])
    final = await handle.wait()
    assert final.state == AggregationState.COMPLETED
    await q.shutdown()


@pytest.mark.asyncio
async def test_shutdown_drains_queued_jobs_so_waiters_do_not_hang(tmp_path):
    """Jobs queued but not yet started should not leave waiters hanging on shutdown."""

    started = asyncio.Event()
    may_finish = asyncio.Event()

    class SlowDownloader(AggregationDownloader):
        async def run(self, camera, target_dir, on_chunk):
            started.set()
            await may_finish.wait()
            (target_dir / camera.local_filename).write_bytes(b"x" * 12)

    q = AggregationQueue(downloader=SlowDownloader())
    await q.start()

    # First job will block on may_finish; second job sits in the queue
    job1 = _make_job(tmp_path / "ep1", "cam_a")
    job2 = _make_job(tmp_path / "ep2", "cam_b")
    handle1 = q.enqueue(job1)
    handle2 = q.enqueue(job2)

    await asyncio.wait_for(started.wait(), timeout=1.0)
    # Now release the in-flight job, then shut down before job2 starts
    may_finish.set()
    await asyncio.wait_for(handle1.wait(), timeout=2.0)

    # Shut down — handle2 must NOT hang
    await q.shutdown()
    final2 = await asyncio.wait_for(handle2.wait(), timeout=1.0)
    assert final2.state == AggregationState.FAILED
    assert "shut down" in (final2.error or "")
