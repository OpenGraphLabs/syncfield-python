"""Tails the Quest's ``/recording/timestamps/{side}`` chunked JSONL response
and emits one :class:`SampleEvent` per successfully-parsed line."""

from __future__ import annotations

import json
import logging
import threading
from typing import Callable, Optional

import httpx

from syncfield.types import SampleEvent


logger = logging.getLogger(__name__)


class TimestampTailReader:
    """Background thread that drives the adapter's ``SampleEvent`` stream."""

    def __init__(
        self,
        *,
        url: str,
        stream_id: str,
        on_sample: Callable[[SampleEvent], None],
        transport: Optional[httpx.BaseTransport] = None,
        clock_domain: str = "remote_quest3",
        uncertainty_ns: int = 10_000_000,
    ) -> None:
        self._url = url
        self._stream_id = stream_id
        self._on_sample = on_sample
        self._transport = transport
        self._clock_domain = clock_domain
        self._uncertainty_ns = uncertainty_ns

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"quest-ts-{self._stream_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        client = httpx.Client(transport=self._transport, timeout=None)
        try:
            with client.stream("GET", self._url) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if self._stop_event.is_set():
                        return
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                        frame_number = int(payload["frame_number"])
                        capture_ns = int(payload["capture_ns"])
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        logger.warning("skipping malformed timestamp line: %r", line)
                        continue
                    self._on_sample(
                        SampleEvent(
                            stream_id=self._stream_id,
                            frame_number=frame_number,
                            capture_ns=capture_ns,
                            channels=None,
                            uncertainty_ns=self._uncertainty_ns,
                            clock_domain=self._clock_domain,
                        )
                    )
        except httpx.HTTPError as exc:  # pragma: no cover — real-Quest path
            logger.warning("timestamp stream closed: %s", exc)
        finally:
            client.close()
