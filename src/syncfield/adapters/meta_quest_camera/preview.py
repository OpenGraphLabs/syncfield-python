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
