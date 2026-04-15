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
StageCallback = Callable[[str, str], None]  # (stream_id, stage_token)


class AggregationDownloader(abc.ABC):
    """Pluggable backend that performs the WiFi switch + OSC download for one camera."""

    @abc.abstractmethod
    async def run(
        self,
        camera: AggregationCameraSpec,
        target_dir: Path,
        on_chunk: ChunkCallback,
        on_stage: Optional[StageCallback] = None,
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
            # Mutable box so chunk_cb and stage_cb share the latest stage;
            # otherwise chunk updates would reset stage to None in the UI.
            stage_box = {"stage": "starting"}

            current = AggregationProgress(
                job_id=job.job_id,
                episode_id=job.episode_id,
                state=AggregationState.RUNNING,
                cameras_total=len(job.cameras),
                cameras_done=sum(1 for c in job.cameras if c.done),
                current_stream_id=camera.stream_id,
                current_bytes=0,
                current_total_bytes=camera.size_bytes,
                stage=stage_box["stage"],
            )
            self._notify(current)

            def chunk_cb(
                stream_id: str, done: int, total: int, *, _cam=camera,
            ) -> None:
                p = AggregationProgress(
                    job_id=job.job_id,
                    episode_id=job.episode_id,
                    state=AggregationState.RUNNING,
                    cameras_total=len(job.cameras),
                    cameras_done=sum(1 for c in job.cameras if c.done),
                    current_stream_id=_cam.stream_id,
                    current_bytes=done,
                    current_total_bytes=total,
                    stage=stage_box["stage"],
                )
                self._notify(p)

            def stage_cb(
                stream_id: str, stage: str, *, _cam=camera,
            ) -> None:
                stage_box["stage"] = stage
                p = AggregationProgress(
                    job_id=job.job_id,
                    episode_id=job.episode_id,
                    state=AggregationState.RUNNING,
                    cameras_total=len(job.cameras),
                    cameras_done=sum(1 for c in job.cameras if c.done),
                    current_stream_id=_cam.stream_id,
                    current_bytes=0,
                    current_total_bytes=0,
                    stage=stage,
                )
                self._notify(p)

            try:
                await self._downloader.run(
                    camera, job.episode_dir, chunk_cb, on_stage=stage_cb,
                )
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
        on_stage: Optional[StageCallback] = None,
    ) -> None:
        _log.info(
            "[aggregation] begin: stream=%s ssid=%r sd=%r",
            camera.stream_id, camera.wifi_ssid, camera.sd_path,
        )

        def stage(tok: str) -> None:
            if on_stage is not None:
                try:
                    on_stage(camera.stream_id, tok)
                except Exception:
                    pass

        prev_ssid = self._switcher.current_ssid()
        _log.info("[aggregation] prev_ssid=%r", prev_ssid)
        already_on_ap = (
            prev_ssid is not None and prev_ssid.lower() == camera.wifi_ssid.lower()
        )

        # Look up the live BLE camera (held open by the matching
        # Go3SStream) so we can pulse it during WiFi association. This is
        # what Insta360's iOS app does — without a recent BLE client the
        # camera puts WiFi into low-power standby and macOS networksetup
        # gets -3925 tmpErr on the join.
        try:
            from ..ble import live_registry as _ble_live_registry
            live_cam = _ble_live_registry.get(camera.ble_address)
        except Exception:
            live_cam = None
            _ble_live_registry = None  # type: ignore[assignment]

        keepalive_task: Optional[asyncio.Task] = None

        async def _keepalive_loop() -> None:
            try:
                while True:
                    if live_cam is not None and live_cam.is_connected:
                        try:
                            await _ble_live_registry.send_wake(live_cam)
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
            except asyncio.CancelledError:
                return

        try:
            if already_on_ap:
                _log.info(
                    "[aggregation] already on camera AP %r — skipping WiFi switch",
                    camera.wifi_ssid,
                )
            else:
                _log.info(
                    "[aggregation] step 1/4: switching WiFi to %r (BLE wake=%s)",
                    camera.wifi_ssid, live_cam is not None,
                )
                stage("switching_wifi")
                # Pre-switch wake: pulse BLE so the camera AP is actively
                # listening before macOS tries to associate.
                if live_cam is not None and _ble_live_registry is not None:
                    try:
                        await _ble_live_registry.send_wake(live_cam)
                        _log.info("[aggregation] BLE wake sent (pre-switch)")
                    except Exception as e:
                        _log.warning("[aggregation] pre-switch wake failed: %s", e)
                    keepalive_task = asyncio.create_task(_keepalive_loop())
                # WiFi switching is synchronous (subprocess); run it off
                # the queue's asyncio loop so we don't block progress
                # callbacks from other jobs that might arrive concurrently.
                await asyncio.to_thread(
                    self._switcher.connect,
                    camera.wifi_ssid,
                    camera.wifi_password,
                )
            _log.info("[aggregation] step 2/4: probing camera at %s", self._ap_host)
            stage("probing")
            osc = await self._wait_for_ap()  # returns the probed client
            local_path = target_dir / camera.local_filename
            _log.info(
                "[aggregation] step 3/4: downloading %s -> %s",
                camera.sd_path, local_path,
            )
            stage("downloading")
            await osc.download(
                remote_path=camera.sd_path,
                local_path=local_path,
                expected_size=camera.size_bytes or None,
                on_progress=lambda done, total: on_chunk(camera.stream_id, done, total),
            )
            _log.info("[aggregation] step 4/4: download done (%s)", local_path)
        finally:
            # Cancel the BLE keep-alive loop first — once the WiFi
            # association is settled (or definitively failed), the camera
            # talks via WiFi and we don't need to keep nudging BLE.
            if keepalive_task is not None:
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except (asyncio.CancelledError, Exception):
                    pass

            # Only restore WiFi if WE switched it. If the user manually
            # connected to the camera AP before clicking Collect Videos,
            # restoring would yank them off without warning — leave the
            # restoration to the user (they can click any network in the
            # macOS WiFi menu, just like they did to join the camera).
            if not already_on_ap:
                try:
                    _log.info("[aggregation] restoring WiFi to %r", prev_ssid)
                    stage("restoring_wifi")
                    await asyncio.to_thread(self._switcher.restore, prev_ssid)
                except Exception:
                    _log.warning(
                        "Go3SAggregationDownloader: failed to restore WiFi to %s",
                        prev_ssid,
                        exc_info=True,
                    )
            else:
                _log.info(
                    "[aggregation] user-controlled WiFi (was on camera AP "
                    "before run) — skipping restore"
                )

    async def _wait_for_ap(self) -> Any:
        """Probe the camera's OSC endpoint until it responds.

        Returns the probed OscHttpClient-like object so the caller can reuse
        it without paying another round of port fallback during list_files /
        download.
        """
        deadline = asyncio.get_running_loop().time() + self._wait_for_ap_timeout
        last_error: Exception | None = None
        for attempt in range(1, self._ap_probe_attempts + 1):
            if asyncio.get_running_loop().time() > deadline:
                break
            try:
                osc = self._osc_factory(self._ap_host)
                info = await osc.probe(timeout=3.0)
                _log.info(
                    "[aggregation] OSC probe ok (attempt %d): model=%r fw=%r",
                    attempt, info.model, info.firmware_version,
                )
                return osc
            except Exception as e:
                last_error = e
                _log.warning(
                    "[aggregation] OSC probe attempt %d failed: %s",
                    attempt, e,
                )
                await asyncio.sleep(self._ap_probe_interval)
        raise RuntimeError(
            f"camera AP unreachable at {self._ap_host} after "
            f"{self._ap_probe_attempts} probes: {last_error}"
        )
