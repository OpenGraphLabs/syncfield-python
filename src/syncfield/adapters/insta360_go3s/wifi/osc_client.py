"""OSC (Open Spherical Camera) HTTP client for Insta360 Go3S.

Targets the Go3S AP (default 192.168.42.1). Endpoints mirror the public
OSC spec: ``/osc/info``, ``/osc/commands/execute`` (``camera.listFiles``),
plus direct file GETs on the SD card paths the camera reports.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import aiohttp


DEFAULT_HOST = "192.168.42.1"
FALLBACK_PORTS: tuple[int, ...] = (80, 6666, 8080)
PROGRESS_CHUNK = 64 * 1024


class OscDownloadError(RuntimeError):
    """Raised when an OSC file download cannot be completed atomically."""


@dataclass(frozen=True)
class OscCameraInfo:
    manufacturer: str
    model: str
    firmware_version: str


@dataclass(frozen=True)
class OscFileEntry:
    name: str
    file_url: str
    size: int


class OscHttpClient:
    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        scheme: str = "http",
        request_timeout: float = 10.0,
    ):
        self._host = host
        self._scheme = scheme
        self._request_timeout = request_timeout

    def _url(self, path: str) -> str:
        return f"{self._scheme}://{self._host}{path}"

    async def probe(self, *, timeout: float = 5.0) -> OscCameraInfo:
        """GET /osc/info on whichever port the camera happens to serve on.

        Insta360 cameras sometimes expose OSC on 80, sometimes 6666 (SDK
        port), depending on firmware. If the host was given without an
        explicit port, try each fallback in sequence and use the first
        that responds. Once the probe lands, ``_host`` is rewritten to
        include the winning port so ``list_files`` and ``download``
        target the same endpoint.
        """
        if ":" in self._host:
            # Host already carries a port — honor it exactly.
            ports_to_try: tuple[int, ...] = ()
        else:
            ports_to_try = FALLBACK_PORTS

        last_error: Exception | None = None
        candidates: list[str] = [self._host] if not ports_to_try else [
            f"{self._host}:{port}" for port in ports_to_try
        ]
        for host_with_port in candidates:
            url = f"{self._scheme}://{host_with_port}/osc/info"
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as s:
                    async with s.get(url) as r:
                        r.raise_for_status()
                        data = await r.json(content_type=None)
                # Stick to the winning host:port for subsequent requests.
                self._host = host_with_port
                return OscCameraInfo(
                    manufacturer=data.get("manufacturer", ""),
                    model=data.get("model", ""),
                    firmware_version=data.get("firmwareVersion", ""),
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                continue
        raise aiohttp.ClientError(
            f"OSC probe failed on all candidates {candidates}: {last_error}"
        )

    async def list_files(self) -> list[OscFileEntry]:
        body = {
            "name": "camera.listFiles",
            "parameters": {"fileType": "video", "entryCount": 100},
        }
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._request_timeout)
        ) as s:
            async with s.post(self._url("/osc/commands/execute"), json=body) as r:
                r.raise_for_status()
                data = await r.json()
        entries = data.get("results", {}).get("entries", [])
        return [
            OscFileEntry(
                name=e.get("name", ""),
                file_url=e.get("fileUrl", ""),
                size=int(e.get("size", 0)),
            )
            for e in entries
        ]

    async def download(
        self,
        *,
        remote_path: str,
        local_path: Path,
        expected_size: int | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        port_overrides: Iterable[int] | None = None,
    ) -> None:
        """Atomically download a file from the camera SD.

        Streams to ``local_path.with_suffix(local_path.suffix + '.part')``
        and renames on success. On any failure (network, size mismatch),
        deletes the partial file and raises :class:`OscDownloadError`.
        """
        partial = local_path.with_suffix(local_path.suffix + ".part")
        partial.parent.mkdir(parents=True, exist_ok=True)
        total = expected_size or 0

        # Determine which ports to try.
        # If self._host already embeds a port (e.g. "127.0.0.1:8765") use only
        # that port so test-server addresses are respected exactly.
        if port_overrides is not None:
            ports = list(port_overrides)
        elif ":" in self._host:
            _, embedded_port = self._host.rsplit(":", 1)
            ports = [int(embedded_port)]
        else:
            ports = list(FALLBACK_PORTS)

        bare_host = self._stripped_host()

        last_error: Exception | None = None
        for port in ports:
            url = f"{self._scheme}://{bare_host}:{port}{remote_path}"
            try:
                await self._stream_to_partial(url, partial, total, on_progress)
                size_on_disk = partial.stat().st_size
                if expected_size is not None and size_on_disk != expected_size:
                    raise OscDownloadError(
                        f"size mismatch: got {size_on_disk}, expected {expected_size}"
                    )
                os.replace(partial, local_path)
                return
            except (aiohttp.ClientError, OscDownloadError, asyncio.TimeoutError) as e:
                last_error = e
                if partial.exists():
                    partial.unlink(missing_ok=True)
                continue

        if partial.exists():
            partial.unlink(missing_ok=True)
        raise OscDownloadError(
            f"all download attempts failed for {remote_path}: {last_error}"
        )

    async def _stream_to_partial(
        self,
        url: str,
        partial: Path,
        expected_total: int,
        on_progress: Callable[[int, int], None] | None,
    ) -> None:
        timeout = aiohttp.ClientTimeout(
            total=None, sock_read=60.0, sock_connect=10.0
        )
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url) as r:
                r.raise_for_status()
                total = expected_total or int(
                    r.headers.get("Content-Length", "0") or 0
                )
                done = 0
                with partial.open("wb") as fh:
                    async for chunk in r.content.iter_chunked(PROGRESS_CHUNK):
                        fh.write(chunk)
                        done += len(chunk)
                        if on_progress is not None:
                            on_progress(done, total)

    def _stripped_host(self) -> str:
        if ":" in self._host:
            return self._host.split(":", 1)[0]
        return self._host
