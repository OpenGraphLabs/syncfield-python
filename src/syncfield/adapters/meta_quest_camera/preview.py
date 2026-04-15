"""MJPEG multipart/x-mixed-replace stream parser + background consumer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Iterator


@dataclass(frozen=True)
class MjpegFrame:
    """One JPEG frame pulled from the Quest's MJPEG preview endpoint."""

    jpeg_bytes: bytes
    capture_ns: int


def _readline(stream: BinaryIO) -> bytes:
    """Read a CRLF-terminated line (bytes, including the CRLF)."""
    line = stream.readline()
    if not line:
        raise EOFError("unexpected end of MJPEG stream")
    return line


def iter_mjpeg_frames(
    stream: BinaryIO, *, boundary: bytes
) -> Iterator[MjpegFrame]:
    """Yield :class:`MjpegFrame` objects from a multipart/x-mixed-replace stream.

    The parser is deliberately strict: it requires both ``Content-Length``
    and ``X-Frame-Capture-Ns`` headers on every part. Malformed parts raise
    :class:`ValueError` so the caller can surface a health event.
    """

    boundary_line = b"--" + boundary
    while True:
        try:
            line = _readline(stream).rstrip(b"\r\n")
        except EOFError:
            return  # clean end of stream between parts
        if not line:
            continue  # skip leading blank lines between parts
        if line != boundary_line:
            raise ValueError(f"expected boundary, got {line!r}")

        headers: dict[str, str] = {}
        while True:
            header_line = _readline(stream).rstrip(b"\r\n")
            if header_line == b"":
                break
            name, _, value = header_line.partition(b":")
            headers[name.strip().lower().decode("ascii")] = (
                value.strip().decode("ascii")
            )

        try:
            length = int(headers["content-length"])
            capture_ns = int(headers["x-frame-capture-ns"])
        except KeyError as exc:
            raise ValueError(f"missing required header: {exc.args[0]}") from exc

        body = stream.read(length)
        if len(body) != length:
            raise EOFError("truncated MJPEG part body")
        # Consume the trailing CRLF.
        stream.readline()
        yield MjpegFrame(jpeg_bytes=body, capture_ns=capture_ns)


import logging
import threading
from typing import Callable, Optional

import httpx


logger = logging.getLogger(__name__)


class MjpegPreviewConsumer:
    """Background thread that pulls the Quest's MJPEG preview into ``latest_frame``.

    The consumer owns its own :class:`httpx.Client` so the main adapter can
    keep its control-plane client free for request/response traffic. When
    ``decode_jpeg=True`` the exposed ``latest_frame`` is a decoded
    ``numpy.ndarray`` (BGR) suitable for the viewer; when ``False`` it is
    the raw :class:`MjpegFrame` — useful for tests that don't want to pull
    in OpenCV.
    """

    def __init__(
        self,
        *,
        url: str,
        boundary: bytes,
        transport: Optional[httpx.BaseTransport] = None,
        decode_jpeg: bool = True,
        on_health: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._url = url
        self._boundary = boundary
        self._transport = transport
        self._decode_jpeg = decode_jpeg
        self._on_health = on_health

        self._lock = threading.Lock()
        self._latest: Optional[object] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------

    @property
    def latest_frame(self) -> Optional[object]:
        with self._lock:
            return self._latest

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="quest-mjpeg-preview", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._consume_once()
            except Exception as exc:  # pragma: no cover - exercised by reconnect test
                logger.warning("MJPEG consumer error: %s", exc)
                if self._on_health is not None:
                    self._on_health("drop", f"mjpeg error: {exc}")
                if self._stop_event.wait(1.0):
                    return

    def _consume_once(self) -> None:
        client = httpx.Client(transport=self._transport, timeout=None)
        try:
            with client.stream("GET", self._url) as response:
                response.raise_for_status()
                # Adapt the streamed bytes to a file-like object for the parser.
                # iter_bytes is used instead of iter_raw so that both real
                # streaming responses and MockTransport (content=) work correctly.
                buffer = _StreamAdapter(response.iter_bytes(8192), self._stop_event)
                for frame in iter_mjpeg_frames(buffer, boundary=self._boundary):
                    decoded: object
                    if self._decode_jpeg:
                        decoded = _decode_jpeg(frame.jpeg_bytes)
                    else:
                        decoded = frame
                    with self._lock:
                        self._latest = decoded
                    if self._stop_event.is_set():
                        return
        finally:
            client.close()


class _StreamAdapter:
    """Adapt an iterator of byte chunks to a file-like ``.readline`` / ``.read``."""

    def __init__(self, source, stop_event: threading.Event) -> None:
        self._source = iter(source)
        self._stop_event = stop_event
        self._buf = bytearray()

    def _pull(self) -> bool:
        if self._stop_event.is_set():
            return False
        try:
            chunk = next(self._source)
        except StopIteration:
            return False
        self._buf.extend(chunk)
        return True

    def read(self, n: int) -> bytes:
        while len(self._buf) < n:
            if not self._pull():
                break
        chunk, self._buf = bytes(self._buf[:n]), self._buf[n:]
        return chunk

    def readline(self) -> bytes:
        while b"\n" not in self._buf:
            if not self._pull():
                break
        idx = self._buf.find(b"\n")
        if idx == -1:
            line, self._buf = bytes(self._buf), bytearray()
        else:
            line, self._buf = bytes(self._buf[: idx + 1]), self._buf[idx + 1 :]
        return line


def _decode_jpeg(data: bytes):
    """Decode JPEG bytes to a BGR ``numpy.ndarray``.

    Uses Pillow (already required by ``syncfield[viewer]``) so the
    adapter can run from a stock ``syncfield[viewer,camera]`` install
    without pulling in OpenCV. Imported lazily so the module stays
    importable on hosts that skip the viewer extra (tests pass
    ``decode_jpeg=False``).
    """
    import io

    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("RGB")
    # The viewer server re-encodes frames assuming BGR (SyncField's
    # house convention across OakCameraStream and UVCWebcamStream),
    # so flip the last axis here.
    return np.asarray(img)[:, :, ::-1]
