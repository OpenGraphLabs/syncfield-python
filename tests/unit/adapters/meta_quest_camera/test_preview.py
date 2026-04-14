"""Unit tests for the MJPEG preview parser + consumer."""

from __future__ import annotations

import io

import pytest

from syncfield.adapters.meta_quest_camera.preview import (
    MjpegFrame,
    iter_mjpeg_frames,
)


BOUNDARY = b"syncfield"


def _part(body: bytes, capture_ns: int) -> bytes:
    headers = (
        f"Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"X-Frame-Capture-Ns: {capture_ns}\r\n\r\n"
    ).encode("ascii")
    return b"--" + BOUNDARY + b"\r\n" + headers + body + b"\r\n"


class TestMjpegParser:
    def test_parses_single_frame(self):
        stream = io.BytesIO(_part(b"\xff\xd8JPEG_BYTES\xff\xd9", capture_ns=42))
        frames = list(iter_mjpeg_frames(stream, boundary=BOUNDARY))
        assert len(frames) == 1
        assert isinstance(frames[0], MjpegFrame)
        assert frames[0].capture_ns == 42
        assert frames[0].jpeg_bytes == b"\xff\xd8JPEG_BYTES\xff\xd9"

    def test_parses_two_frames_in_stream(self):
        data = _part(b"FRAME_1", 1) + _part(b"FRAME_2", 2)
        frames = list(iter_mjpeg_frames(io.BytesIO(data), boundary=BOUNDARY))
        assert [f.jpeg_bytes for f in frames] == [b"FRAME_1", b"FRAME_2"]
        assert [f.capture_ns for f in frames] == [1, 2]

    def test_malformed_part_raises(self):
        # Missing Content-Length header.
        data = b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\n\r\nbody\r\n"
        with pytest.raises(ValueError):
            list(iter_mjpeg_frames(io.BytesIO(data), boundary=BOUNDARY))
