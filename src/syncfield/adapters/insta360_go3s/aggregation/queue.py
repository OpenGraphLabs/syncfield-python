"""Background aggregation queue for Insta360 Go3S episodes.

A single asyncio worker processes :class:`AggregationJob`s in FIFO order.
Per-camera atomicity: a failed download leaves no partial files for that
camera. Per-episode atomicity: a job is COMPLETED only when every camera
succeeds; otherwise FAILED with per-camera breakdown for selective retry.
"""
from __future__ import annotations

import abc
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .types import (
    AggregationCameraSpec,
    AggregationJob,
    AggregationProgress,
    AggregationState,
)

_log = logging.getLogger(__name__)

ProgressListener = Callable[[AggregationProgress], None]
ChunkCallback = Callable[[str, int, int], None]  # (stream_id, done, total)


class AggregationDownloader(abc.ABC):
    """Pluggable backend that performs the WiFi switch + OSC download for one camera."""

    @abc.abstractmethod
    async def run(
        self,
        camera: AggregationCameraSpec,
        target_dir: Path,
        on_chunk: ChunkCallback,
    ) -> None: ...


@dataclass
class _JobHandle:
    job: AggregationJob
    done: asyncio.Event
    final_progress: Optional[AggregationProgress] = None

    async def wait(self) -> AggregationProgress:
        await self.done.wait()
        if self.final_progress is None:
            raise RuntimeError(
                f"AggregationJob {self.job.job_id}: done event set but final_progress unpopulated"
            )
        return self.final_progress


class AggregationQueue:
    def __init__(self, *, downloader: AggregationDownloader):
        self._downloader = downloader
        self._queue: asyncio.Queue[Optional[_JobHandle]] = asyncio.Queue()
        self._handles: dict[str, _JobHandle] = {}
        self._listeners: list[ProgressListener] = []
        self._worker_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._stop.clear()
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def shutdown(self) -> None:
        self._stop.set()
        await self._queue.put(None)
        if self._worker_task is not None:
            await self._worker_task
            self._worker_task = None

    def subscribe(self, listener: ProgressListener) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: ProgressListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def enqueue(self, job: AggregationJob) -> _JobHandle:
        handle = _JobHandle(job=job, done=asyncio.Event())
        self._handles[job.job_id] = handle
        job.write_manifest()
        self._queue.put_nowait(handle)
        return handle

    def retry(self, job_id: str) -> _JobHandle:
        handle = self._handles.get(job_id)
        if handle is None:
            raise KeyError(job_id)
        handle.job.state = AggregationState.PENDING
        handle.job.error = None
        handle.done = asyncio.Event()
        handle.final_progress = None
        handle.job.write_manifest()
        self._queue.put_nowait(handle)
        return handle

    def status(self, job_id: str) -> Optional[AggregationProgress]:
        handle = self._handles.get(job_id)
        return handle.final_progress if handle else None

    def recover_from_disk(self, *, search_root: Path) -> list[AggregationJob]:
        recovered: list[AggregationJob] = []
        for manifest in search_root.rglob("aggregation.json"):
            try:
                data = json.loads(manifest.read_text())
                job = AggregationJob.from_dict(data)
            except Exception:
                continue
            if job.state in (AggregationState.PENDING, AggregationState.RUNNING):
                job.state = AggregationState.PENDING
                recovered.append(job)
        return recovered

    async def _worker_loop(self) -> None:
        while not self._stop.is_set():
            handle = await self._queue.get()
            if handle is None:
                break
            if self._stop.is_set():
                # Shutdown raced with queue.get(): fail this handle via the drain path.
                self._fail_queued_handle(handle)
                break
            try:
                await self._run_job(handle)
            except Exception as e:
                handle.job.state = AggregationState.FAILED
                handle.job.error = f"worker crash: {e}"
                handle.job.write_manifest()
                final = self._snapshot(handle.job)
                handle.final_progress = final
                self._notify(final)
                handle.done.set()
            # Yield to the loop so a pending shutdown() caller can observe
            # the completion and set _stop before we pick up the next job.
            await asyncio.sleep(0)
        # Drain any remaining queued jobs so their waiters don't hang
        while not self._queue.empty():
            try:
                handle = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if handle is None:
                continue
            self._fail_queued_handle(handle)

    def _fail_queued_handle(self, handle: _JobHandle) -> None:
        handle.job.state = AggregationState.FAILED
        handle.job.error = "queue shut down before job started"
        handle.job.write_manifest()
        handle.final_progress = self._snapshot(handle.job)
        self._notify(handle.final_progress)
        handle.done.set()

    async def _run_job(self, handle: _JobHandle) -> None:
        job = handle.job
        job.state = AggregationState.RUNNING
        job.started_at_ns = time.monotonic_ns()
        job.error = None
        job.write_manifest()
        self._notify(self._snapshot(job))

        any_failure = False
        for camera in job.cameras:
            if camera.done:
                continue
            current = AggregationProgress(
                job_id=job.job_id,
                episode_id=job.episode_id,
                state=AggregationState.RUNNING,
                cameras_total=len(job.cameras),
                cameras_done=sum(1 for c in job.cameras if c.done),
                current_stream_id=camera.stream_id,
                current_bytes=0,
                current_total_bytes=camera.size_bytes,
            )
            self._notify(current)

            def chunk_cb(stream_id: str, done: int, total: int, *, _cam=camera) -> None:
                p = AggregationProgress(
                    job_id=job.job_id,
                    episode_id=job.episode_id,
                    state=AggregationState.RUNNING,
                    cameras_total=len(job.cameras),
                    cameras_done=sum(1 for c in job.cameras if c.done),
                    current_stream_id=_cam.stream_id,
                    current_bytes=done,
                    current_total_bytes=total,
                )
                self._notify(p)

            try:
                await self._downloader.run(camera, job.episode_dir, chunk_cb)
                camera.done = True
                camera.error = None
            except Exception as e:
                camera.done = False
                camera.error = str(e)
                any_failure = True
            job.write_manifest()

        job.completed_at_ns = time.monotonic_ns()
        if any_failure:
            job.state = AggregationState.FAILED
            failed_ids = [c.stream_id for c in job.cameras if not c.done]
            job.error = f"failed cameras: {failed_ids}"
        else:
            job.state = AggregationState.COMPLETED
            job.error = None
        job.write_manifest()

        final = self._snapshot(job)
        handle.final_progress = final
        self._notify(final)
        handle.done.set()

    def _snapshot(self, job: AggregationJob) -> AggregationProgress:
        return AggregationProgress(
            job_id=job.job_id,
            episode_id=job.episode_id,
            state=job.state,
            cameras_total=len(job.cameras),
            cameras_done=sum(1 for c in job.cameras if c.done),
            current_stream_id=None,
            current_bytes=0,
            current_total_bytes=0,
            error=job.error,
        )

    def _notify(self, progress: AggregationProgress) -> None:
        for listener in list(self._listeners):
            try:
                listener(progress)
            except Exception:
                # Do not let a buggy listener take down the worker.
                _log.warning(
                    "AggregationQueue listener raised", exc_info=True
                )


def make_job_id() -> str:
    return f"agg_{uuid.uuid4().hex[:12]}"


class Go3SAggregationDownloader(AggregationDownloader):
    """Production downloader: switch WiFi -> probe OSC -> download -> restore.

    Always restores the previous WiFi network in a finally block, even when
    the download fails. The restore step is best-effort — if it fails, a
    warning is logged but the original download error is preserved for the
    caller.
    """

    def __init__(
        self,
        *,
        switcher: Any,                                # WifiSwitcher
        osc_factory: Callable[[str], Any],            # (host) -> OscHttpClient-like
        ap_host: str = "192.168.42.1",
        wait_for_ap_timeout: float = 30.0,
        ap_probe_attempts: int = 6,
        ap_probe_interval: float = 5.0,
    ):
        self._switcher = switcher
        self._osc_factory = osc_factory
        self._ap_host = ap_host
        self._wait_for_ap_timeout = wait_for_ap_timeout
        self._ap_probe_attempts = ap_probe_attempts
        self._ap_probe_interval = ap_probe_interval

    async def run(
        self,
        camera: AggregationCameraSpec,
        target_dir: Path,
        on_chunk: ChunkCallback,
    ) -> None:
        prev_ssid = self._switcher.current_ssid()
        try:
            self._switcher.connect(camera.wifi_ssid, camera.wifi_password)
            await self._wait_for_ap()
            osc = self._osc_factory(self._ap_host)
            await osc.probe(timeout=5.0)
            local_path = target_dir / camera.local_filename
            await osc.download(
                remote_path=camera.sd_path,
                local_path=local_path,
                expected_size=camera.size_bytes or None,
                on_progress=lambda done, total: on_chunk(camera.stream_id, done, total),
            )
        finally:
            try:
                self._switcher.restore(prev_ssid)
            except Exception:
                _log.warning(
                    "Go3SAggregationDownloader: failed to restore WiFi to %s",
                    prev_ssid,
                    exc_info=True,
                )

    async def _wait_for_ap(self) -> None:
        deadline = asyncio.get_event_loop().time() + self._wait_for_ap_timeout
        last_error: Exception | None = None
        for _ in range(self._ap_probe_attempts):
            if asyncio.get_event_loop().time() > deadline:
                break
            try:
                osc = self._osc_factory(self._ap_host)
                await osc.probe(timeout=2.0)
                return
            except Exception as e:
                last_error = e
                await asyncio.sleep(self._ap_probe_interval)
        raise RuntimeError(f"camera AP unreachable: {last_error}")
