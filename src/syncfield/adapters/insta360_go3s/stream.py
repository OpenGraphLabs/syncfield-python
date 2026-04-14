"""Insta360 Go3S Stream — BLE trigger + deferred WiFi aggregation."""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)

from syncfield.clock import SessionClock
from syncfield.stream import StreamBase
from syncfield.types import (
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    StreamCapabilities,
)

from .aggregation.queue import (
    AggregationQueue,
    Go3SAggregationDownloader,
    make_job_id,
)
from .aggregation.types import (
    AggregationCameraSpec,
    AggregationJob,
    AggregationState,
)
from .ble.camera import Go3SBLECamera
from .wifi.osc_client import OscHttpClient
from .wifi.switcher import wifi_switcher_for_platform


AggregationPolicy = Literal["eager", "on_demand"]

_QUEUE_LOCK = threading.Lock()
_QUEUE: Optional[AggregationQueue] = None
_QUEUE_LOOP: Optional[asyncio.AbstractEventLoop] = None
_QUEUE_THREAD: Optional[threading.Thread] = None


def _global_aggregation_queue() -> AggregationQueue:
    """Lazy singleton: queue + dedicated background thread + dedicated loop.

    The thread runs the asyncio loop forever (daemon thread, dies with process).
    All queue interactions from outside threads must be marshaled via
    ``asyncio.run_coroutine_threadsafe(..., _QUEUE_LOOP)`` because
    ``asyncio.Queue`` is not thread-safe.
    """
    global _QUEUE, _QUEUE_LOOP, _QUEUE_THREAD
    with _QUEUE_LOCK:
        if _QUEUE is not None:
            return _QUEUE
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run_loop() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        thread = threading.Thread(
            target=_run_loop,
            name="go3s-aggregation",
            daemon=True,
        )
        thread.start()
        ready.wait(timeout=5.0)

        switcher = wifi_switcher_for_platform()
        downloader = Go3SAggregationDownloader(
            switcher=switcher,
            osc_factory=lambda host: OscHttpClient(host=host),
        )
        queue = AggregationQueue(downloader=downloader)
        # Start the worker on the dedicated loop and wait for it to be running.
        fut: Future = asyncio.run_coroutine_threadsafe(queue.start(), loop)
        fut.result(timeout=5.0)

        _QUEUE_LOOP = loop
        _QUEUE_THREAD = thread
        _QUEUE = queue

        # Crash-recovery scan is opt-in via SYNCFIELD_GO3S_RECOVERY_ROOT.
        # It runs on a background thread so singleton init stays sub-second
        # even when the user points it at a huge tree (e.g. project root
        # with .git/.venv/node_modules — rglob("aggregation.json") across
        # that can take seconds and would stall the caller's enqueue path).
        recovery_env = os.environ.get("SYNCFIELD_GO3S_RECOVERY_ROOT")
        if recovery_env:
            recovery_root = Path(recovery_env)
            threading.Thread(
                target=_run_recovery_scan,
                args=(queue, recovery_root, loop),
                name="go3s-recovery",
                daemon=True,
            ).start()
    return _QUEUE


async def _enqueue_async(queue: AggregationQueue, job: AggregationJob) -> None:
    queue.enqueue(job)


def _run_recovery_scan(
    queue: AggregationQueue,
    recovery_root: Path,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Walk recovery_root for aggregation.json files and re-enqueue pending jobs.

    Runs on its own thread so it doesn't block singleton init. Enqueue calls
    are marshaled back onto the queue's dedicated loop.
    """
    try:
        if not recovery_root.exists():
            return
        recovered = queue.recover_from_disk(search_root=recovery_root)
        for job in recovered:
            try:
                fut: Future = asyncio.run_coroutine_threadsafe(
                    _enqueue_async(queue, job), loop
                )
                fut.result(timeout=5.0)
            except Exception:
                logger.exception(
                    "Failed to re-enqueue recovered job %s", job.job_id
                )
        if recovered:
            logger.info(
                "Go3S aggregation: recovered %d pending job(s) from %s",
                len(recovered), recovery_root,
            )
    except Exception:
        logger.exception("Go3S aggregation recovery scan failed")


def _enqueue_on_global_queue(job: AggregationJob) -> None:
    """Thread-safe enqueue marshaled onto the queue's owned loop.

    When ``_global_aggregation_queue`` has been monkeypatched (unit tests),
    ``_QUEUE_LOOP`` may be ``None``; in that case we call ``enqueue`` directly
    on the (mock) queue since there's no dedicated loop to marshal onto.
    """
    queue = _global_aggregation_queue()
    if _QUEUE_LOOP is None:
        queue.enqueue(job)
        return
    fut: Future = asyncio.run_coroutine_threadsafe(
        _enqueue_async(queue, job), _QUEUE_LOOP
    )
    # 30 s headroom: the actual enqueue is O(1), but if the queue's owned
    # loop is busy dispatching an earlier job's progress callbacks the
    # marshaled coroutine may queue behind them for a few seconds on a
    # slow host. 5 s was too tight on a loaded dev machine.
    fut.result(timeout=30.0)


class Go3SStream(StreamBase):
    """Insta360 Go3S adapter — wireless start/stop + background aggregation.

    Args:
        stream_id: Stream id.
        ble_address: BLE MAC (or platform UUID on macOS) of the Go3S camera.
        output_dir: Episode directory; aggregated files land here.
        aggregation_policy: v1 supports two policies — ``"eager"`` (default;
            enqueue immediately on stop) and ``"on_demand"`` (enqueue only
            when the viewer or caller explicitly triggers via
            :attr:`pending_aggregation_job`).
        wifi_ssid: Camera AP SSID. Auto-derived from ``ble_address`` on first
            connect when omitted.
        wifi_password: Camera AP password (Insta360 default is ``"88888888"``).
    """

    _discovery_kind = "video"
    _discovery_adapter_type = "insta360_go3s"

    def __init__(
        self,
        stream_id: str,
        *,
        ble_address: str,
        output_dir: Path,
        aggregation_policy: AggregationPolicy = "on_demand",
        wifi_ssid: Optional[str] = None,
        wifi_password: str = "88888888",
    ):
        super().__init__(
            id=stream_id,
            kind="video",
            capabilities=StreamCapabilities(
                provides_audio_track=False,
                supports_precise_timestamps=False,
                is_removable=True,
                produces_file=True,
                live_preview=False,
            ),
        )
        self._ble_address = ble_address
        self._output_dir = Path(output_dir)
        self._aggregation_policy: AggregationPolicy = aggregation_policy
        self._wifi_ssid = wifi_ssid  # auto-derived from BLE addr on first connect
        self._wifi_password = wifi_password
        self._start_ack_ns: Optional[int] = None
        self._stop_ack_ns: Optional[int] = None
        self._sd_path: Optional[str] = None
        # Persistent BLE session — opened at connect(), held through start/stop,
        # closed at disconnect(). Without this the SYNC + CHECK_AUTH handshake
        # (~4–7 s on macOS) would fire on every start_recording / stop_recording
        # call, adding intolerable latency to the Record / Stop UX.
        self._cam: Optional[Go3SBLECamera] = None
        self._ble_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ble_thread: Optional[threading.Thread] = None
        self._ble_lock = threading.Lock()
        # Last aggregation state we emitted a health event for; used to
        # avoid spamming progress-every-chunk events — we only emit on
        # state transitions (running/completed/failed) and phase changes.
        self._last_agg_state_emitted: Optional[str] = None
        self._agg_listener = None  # type: ignore[assignment]
        self.pending_aggregation_job: Optional[AggregationJob] = None
        """Last job built by stop_recording() — set after each stop, never cleared.

        For on_demand policy this is the handle the caller uses to trigger
        aggregation manually. For eager policy it's set as a side effect but
        the worker will already have it; reading it is mostly diagnostic.
        """

    # ----- Stream protocol -----

    @property
    def device_key(self):  # type: ignore[override]
        return ("go3s", self._ble_address)

    def prepare(self) -> None:
        self._emit_health(
            HealthEvent(
                stream_id=self.id,
                kind=HealthEventKind.HEARTBEAT,
                at_ns=time.monotonic_ns(),
                detail="Go3S prepared",
            )
        )

    def connect(self) -> None:
        """Open a persistent BLE session (idempotent).

        The SYNC + CHECK_AUTH handshake is performed here so that
        ``start_recording`` / ``stop_recording`` can dispatch their commands
        over an already-authenticated link (< 1 s per call instead of 4–7 s).
        """
        if self._cam is not None and self._cam.is_connected:
            return
        # Defensive teardown of any stale instance before opening a new one.
        self._force_disconnect_cam()

        cam = Go3SBLECamera(self._ble_address)
        try:
            self._run_on_ble_loop(
                # Budget: per attempt = 8s scan + 15s BleakClient.connect +
                # 3s sync + 3s auth ≈ 29s. With one retry on failure (macOS
                # CoreBluetooth stuck-state recovery) and a 0.5s inter-attempt
                # sleep, worst case ≈ 60s. In practice the common path (fresh
                # process, advertising peripheral) is 3–5s total.
                cam.connect(
                    sync_timeout=3.0,
                    auth_timeout=3.0,
                    discovery_timeout=8.0,
                ),
                timeout=60.0,
            )
        except Exception as e:
            self._emit_health(HealthEvent(
                stream_id=self.id,
                kind=HealthEventKind.ERROR,
                at_ns=time.monotonic_ns(),
                detail=f"Go3S BLE connect failed: {e}",
            ))
            raise

        self._cam = cam

        # Derive the WiFi AP SSID from the BLE advertised name. Insta360's
        # iOS SDK pattern: SSID = "{ble_name}.OSC" (with the exact spacing of
        # the BLE name preserved, e.g. "GO 3S 1TEBJJ.OSC"). The user can
        # override this by passing wifi_ssid=... to the constructor.
        if self._wifi_ssid is None:
            ble_name = cam.ble_name
            if ble_name:
                self._wifi_ssid = (
                    ble_name if ble_name.endswith(".OSC") else f"{ble_name}.OSC"
                )
            else:
                self._emit_health(HealthEvent(
                    stream_id=self.id,
                    kind=HealthEventKind.WARNING,
                    at_ns=time.monotonic_ns(),
                    detail=(
                        "Go3S BLE connected but no advertised name — pass "
                        "wifi_ssid=... to Go3SStream for aggregation to work."
                    ),
                ))

        self._emit_health(HealthEvent(
            stream_id=self.id,
            kind=HealthEventKind.HEARTBEAT,
            at_ns=time.monotonic_ns(),
            detail=(
                f"Go3S BLE link open and authed; WiFi SSID="
                f"{self._wifi_ssid!r}"
            ),
        ))

        # Subscribe to the global aggregation queue so job state changes
        # for THIS stream surface in the stream's health log.
        self._subscribe_to_aggregation_queue()

    def start_recording(self, session_clock: SessionClock) -> None:
        """Fire BLE start_capture over the persistent link.

        If the link has dropped since connect(), reconnect once before sending.
        """
        self._ensure_ble_connected()
        assert self._cam is not None
        self._start_ack_ns = self._run_on_ble_loop(
            self._cam.start_capture(),
            timeout=5.0,
        )

    def stop_recording(self) -> FinalizationReport:
        """Fire BLE stop_capture; retry once with reconnect if the first attempt fails.

        If both attempts fail, return ``status="failed"`` with a clear error so
        the orchestrator and viewer reflect that the device may still be recording
        and needs manual intervention. Aggregation is NOT enqueued in that case
        (the SD file path is unknown).
        """
        result_or_error = self._attempt_stop()
        if isinstance(result_or_error, Exception):
            # One retry with a fresh BLE connection.
            self._emit_health(HealthEvent(
                stream_id=self.id,
                kind=HealthEventKind.RECONNECT,
                at_ns=time.monotonic_ns(),
                detail=f"Go3S stop failed, retrying with fresh BLE: {result_or_error}",
            ))
            self._force_disconnect_cam()
            try:
                self.connect()
            except Exception as e_reconnect:
                return self._failed_report(
                    f"stop_recording: reconnect for retry failed ({e_reconnect}); "
                    f"original error: {result_or_error}. "
                    "Camera may still be recording — stop it manually via the "
                    "Insta360 app or the camera button."
                )
            result_or_error = self._attempt_stop()
            if isinstance(result_or_error, Exception):
                return self._failed_report(
                    f"stop_recording failed after retry: {result_or_error}. "
                    "Camera may still be recording — stop it manually via the "
                    "Insta360 app or the camera button."
                )

        self._stop_ack_ns = result_or_error.ack_host_ns
        self._sd_path = result_or_error.file_path

        if not self._sd_path:
            return self._failed_report(
                "stop_recording did not return a file path; the BLE STOP "
                "response did not contain a /DCIM/... entry. Verify the "
                "camera is in video mode."
            )

        try:
            job = self._build_job()
        except RuntimeError as e:
            return self._failed_report(str(e))

        self.pending_aggregation_job = job

        # Always persist the aggregation manifest to disk so a later
        # Sync (including after viewer restart) can find the episode.
        # For the wrist-mount workflow the camera's WiFi is typically
        # OFF during recording; the user docks the camera later and
        # clicks Sync in the viewer, at which point recovery_from_disk
        # + the queue pick up every pending job.
        try:
            job.write_manifest()
        except Exception as e:
            logger.warning(
                "Go3SStream(%s): could not persist aggregation manifest: %s",
                self.id, e,
            )

        if self._aggregation_policy == "eager":
            try:
                self._enqueue_job(job)
            except Exception as e:
                return self._failed_report(
                    f"Failed to enqueue aggregation job: {e}. "
                    "The camera stopped recording successfully, but the "
                    "background download queue is unreachable. Retry from "
                    "the viewer once the queue is healthy."
                )
            self._emit_health(HealthEvent(
                stream_id=self.id,
                kind=HealthEventKind.HEARTBEAT,
                at_ns=time.monotonic_ns(),
                detail=(
                    f"Aggregation enqueued (job={job.job_id}, "
                    f"ssid={self._wifi_ssid!r}, sd={self._sd_path!r})"
                ),
            ))
        else:
            # on_demand: persist only; the user will trigger Sync later.
            self._emit_health(HealthEvent(
                stream_id=self.id,
                kind=HealthEventKind.HEARTBEAT,
                at_ns=time.monotonic_ns(),
                detail=(
                    f"Aggregation pending — queued for manual Sync "
                    f"(job={job.job_id}, sd={self._sd_path!r})"
                ),
            ))
        return FinalizationReport(
            stream_id=self.id,
            status="pending_aggregation",
            frame_count=0,
            file_path=None,
            first_sample_at_ns=self._start_ack_ns,
            last_sample_at_ns=self._stop_ack_ns,
            health_events=list(self._collected_health),
            error=None,
        )

    def _subscribe_to_aggregation_queue(self) -> None:
        """Bridge global aggregation queue → per-stream health log.

        The global queue emits progress for all jobs in all streams. We
        filter to events mentioning *this* stream's id and only emit a
        health event on meaningful state transitions (running / completed
        / failed / failed-to-connect) — never per-chunk progress, which
        would spam the sidebar.
        """
        if self._agg_listener is not None:
            return
        try:
            from .aggregation.types import AggregationState as _AS
        except ImportError:
            return

        def on_progress(progress) -> None:
            if progress.current_stream_id and progress.current_stream_id != self.id:
                return
            state_value = (
                progress.state.value
                if hasattr(progress.state, "value")
                else str(progress.state)
            )
            # Dedup per (job, state) so we only emit on real transitions.
            key = f"{progress.job_id}:{state_value}"
            if key == self._last_agg_state_emitted:
                return
            self._last_agg_state_emitted = key

            if state_value == "running":
                kind = HealthEventKind.HEARTBEAT
                detail = f"Aggregation running (job={progress.job_id})"
            elif state_value == "completed":
                kind = HealthEventKind.HEARTBEAT
                detail = f"Aggregation completed (job={progress.job_id})"
            elif state_value == "failed":
                kind = HealthEventKind.ERROR
                detail = (
                    f"Aggregation failed (job={progress.job_id}): "
                    f"{progress.error or 'unknown error'}"
                )
            else:
                return

            try:
                self._emit_health(HealthEvent(
                    stream_id=self.id,
                    kind=kind,
                    at_ns=time.monotonic_ns(),
                    detail=detail,
                ))
            except Exception:
                # Listener callbacks run on the aggregation thread; never
                # let a health-emit error take down the worker.
                logger.warning(
                    "Go3SStream: failed to emit aggregation health event",
                    exc_info=True,
                )

        try:
            _global_aggregation_queue().subscribe(on_progress)
            self._agg_listener = on_progress
        except Exception:
            logger.warning(
                "Go3SStream: could not subscribe to aggregation queue",
                exc_info=True,
            )

    def _unsubscribe_from_aggregation_queue(self) -> None:
        if self._agg_listener is None:
            return
        try:
            _global_aggregation_queue().unsubscribe(self._agg_listener)
        except Exception:
            pass
        self._agg_listener = None

    def disconnect(self) -> None:
        """Close the BLE session and tear down the BLE event loop thread."""
        self._unsubscribe_from_aggregation_queue()
        self._force_disconnect_cam()
        with self._ble_lock:
            loop = self._ble_loop
            thread = self._ble_thread
            self._ble_loop = None
            self._ble_thread = None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread is not None:
            thread.join(timeout=2.0)

    # ----- internals -----

    def _ensure_ble_connected(self) -> None:
        """If connect() was skipped or the link dropped, reopen transparently."""
        if self._cam is not None and self._cam.is_connected:
            return
        logger.warning(
            "Go3SStream(%s): BLE link not open at command time; reconnecting",
            self.id,
        )
        self._force_disconnect_cam()
        self.connect()

    def _attempt_stop(self):
        """Run a single stop_capture attempt. Returns CaptureResult or the Exception."""
        try:
            self._ensure_ble_connected()
            assert self._cam is not None
            return self._run_on_ble_loop(
                self._cam.stop_capture(),
                timeout=10.0,
            )
        except Exception as e:
            return e

    def _failed_report(self, error_message: str) -> FinalizationReport:
        """Build a FinalizationReport with status='failed' and surface a health event."""
        self._emit_health(HealthEvent(
            stream_id=self.id,
            kind=HealthEventKind.ERROR,
            at_ns=time.monotonic_ns(),
            detail=error_message,
        ))
        return FinalizationReport(
            stream_id=self.id,
            status="failed",
            frame_count=0,
            file_path=None,
            first_sample_at_ns=self._start_ack_ns,
            last_sample_at_ns=self._stop_ack_ns,
            health_events=list(self._collected_health),
            error=error_message,
        )

    def _force_disconnect_cam(self) -> None:
        """Best-effort teardown of the cached BLE client; swallow errors."""
        cam = self._cam
        self._cam = None
        if cam is None:
            return
        try:
            self._run_on_ble_loop(cam.disconnect(), timeout=3.0)
        except Exception:
            pass

    def _ensure_ble_loop(self) -> asyncio.AbstractEventLoop:
        """Lazy-create a dedicated daemon thread running an asyncio loop for BLE."""
        with self._ble_lock:
            loop = self._ble_loop
            thread = self._ble_thread
            if loop is not None and loop.is_running():
                return loop

            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _run() -> None:
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()
                # run_forever returns when loop.stop() is called; close the loop.
                loop.close()

            thread = threading.Thread(
                target=_run,
                name=f"go3s-ble-{self.id}",
                daemon=True,
            )
            thread.start()
            ready.wait(timeout=2.0)
            self._ble_loop = loop
            self._ble_thread = thread
            return loop

    def _run_on_ble_loop(self, coro, *, timeout: float):
        """Marshal a coroutine onto the stream's private BLE loop + wait."""
        loop = self._ensure_ble_loop()
        fut: Future = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)

    def _build_job(self) -> AggregationJob:
        if not self._sd_path:
            raise RuntimeError(
                "stop_recording did not return a file path; the BLE STOP response "
                "did not contain a /DCIM/... entry. Verify the camera is in video "
                "mode and reachable."
            )
        if not self._wifi_ssid:
            raise RuntimeError(
                "Cannot build aggregation job without a WiFi SSID. BLE name "
                "was not captured during connect(); pass wifi_ssid=... to the "
                "Go3SStream constructor (expected format: 'GO 3S XXXXXX.OSC')."
            )
        ext = ".mp4" if self._sd_path.lower().endswith(".mp4") else ".insv"
        camera_spec = AggregationCameraSpec(
            stream_id=self.id,
            ble_address=self._ble_address,
            wifi_ssid=self._wifi_ssid,
            wifi_password=self._wifi_password,
            sd_path=self._sd_path,
            local_filename=f"{self.id}{ext}",
            size_bytes=0,  # populated by OSC listFiles in production downloader
        )
        return AggregationJob(
            job_id=make_job_id(),
            episode_id=self._output_dir.name,
            episode_dir=self._output_dir,
            cameras=[camera_spec],
            state=AggregationState.PENDING,
        )

    def _enqueue_job(self, job: AggregationJob) -> None:
        _enqueue_on_global_queue(job)


    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> list:
        """Enumerate Go3S cameras currently advertising over BLE.

        Filters by case-insensitive ``"go 3"`` / ``"go3"`` substring on the
        advertised name. Each result has ``construct_kwargs`` pre-populated
        with the BLE address so the discovery modal can build a working
        :class:`Go3SStream` without further user input.
        """
        from syncfield.discovery import DiscoveredDevice
        from syncfield.discovery._ble import scan_peripherals

        peripherals = scan_peripherals(timeout=timeout)
        results = []
        for peripheral in peripherals:
            name = (getattr(peripheral, "name", None) or "").strip()
            lowered = name.lower()
            if "go 3" not in lowered and "go3" not in lowered:
                continue
            address = getattr(peripheral, "address", None) or ""
            results.append(
                DiscoveredDevice(
                    adapter_type="insta360_go3s",
                    adapter_cls=cls,
                    kind="video",
                    display_name=name or "Insta360 Go3S",
                    description=(
                        f"Insta360 Go3S · {address[:8]}…"
                        if address
                        else "Insta360 Go3S"
                    ),
                    device_id=address or name,
                    construct_kwargs={
                        "ble_address": address,
                    },
                )
            )
        return results

