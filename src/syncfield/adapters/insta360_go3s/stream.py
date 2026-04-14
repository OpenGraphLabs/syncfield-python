"""Insta360 Go3S Stream — BLE trigger + deferred WiFi aggregation."""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Literal, Optional

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
    return _QUEUE


async def _enqueue_async(queue: AggregationQueue, job: AggregationJob) -> None:
    queue.enqueue(job)


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
    fut.result(timeout=5.0)


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
        aggregation_policy: AggregationPolicy = "eager",
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
        # Quick BLE handshake to verify reachability + auto-derive SSID; then disconnect.
        self._run_async(self._verify_reachable())

    def start_recording(self, session_clock: SessionClock) -> None:
        self._run_async(self._do_start())

    def stop_recording(self) -> FinalizationReport:
        self._run_async(self._do_stop())
        job = self._build_job()
        self.pending_aggregation_job = job
        if self._aggregation_policy == "eager":
            self._enqueue_job(job)
        # "on_demand": leave job pending; orchestrator/viewer triggers later.
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

    def disconnect(self) -> None:
        # Aggregation runs independently; nothing to tear down synchronously.
        pass

    # ----- internals -----

    async def _verify_reachable(self) -> None:
        cam = Go3SBLECamera(self._ble_address)
        await cam.connect(sync_timeout=2.0, auth_timeout=1.0)
        if self._wifi_ssid is None:
            self._wifi_ssid = self._derive_ssid_from_address(self._ble_address)
        await cam.disconnect()

    async def _do_start(self) -> None:
        cam = Go3SBLECamera(self._ble_address)
        await cam.connect()
        try:
            self._start_ack_ns = await cam.start_capture()
        finally:
            await cam.disconnect()

    async def _do_stop(self) -> None:
        cam = Go3SBLECamera(self._ble_address)
        await cam.connect()
        try:
            result = await cam.stop_capture()
            self._stop_ack_ns = result.ack_host_ns
            self._sd_path = result.file_path
        finally:
            await cam.disconnect()

    def _build_job(self) -> AggregationJob:
        if self._sd_path is None:
            raise RuntimeError("stop_recording did not return a file path")
        ext = ".mp4" if self._sd_path.lower().endswith(".mp4") else ".insv"
        camera_spec = AggregationCameraSpec(
            stream_id=self.id,
            ble_address=self._ble_address,
            wifi_ssid=self._wifi_ssid or self._derive_ssid_from_address(self._ble_address),
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

    @staticmethod
    def _derive_ssid_from_address(address: str) -> str:
        suffix = address.replace(":", "").upper()[-12:]
        return f"Go3S-{suffix}.OSC"

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

    def _run_async(self, coro) -> None:
        """Bridge sync Stream API to the async BLE helper.

        Each call creates a fresh asyncio loop. If called from inside an
        already-running loop (e.g., from an async test), ``asyncio.run()``
        will raise ``RuntimeError`` — that's intentional; callers must
        arrange a thread boundary themselves.
        """
        asyncio.run(coro)
