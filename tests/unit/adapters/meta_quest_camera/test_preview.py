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


import time

import httpx

from syncfield.adapters.meta_quest_camera.preview import MjpegPreviewConsumer


def _mjpeg_transport(parts: list[bytes]):
    body = b"".join(parts)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=syncfield",
            },
            content=body,
        )

    return httpx.MockTransport(handler)


class TestMjpegPreviewConsumer:
    def test_updates_latest_frame(self):
        # Valid JPEG magic bytes — cv2.imdecode will accept this minimally.
        # For the unit test we skip decoding and test the raw-bytes path.
        parts = [_part(b"\xff\xd8ONE\xff\xd9", 100), _part(b"\xff\xd8TWO\xff\xd9", 200)]
        transport = _mjpeg_transport(parts)

        consumer = MjpegPreviewConsumer(
            url="http://test/preview/left",
            boundary=b"syncfield",
            transport=transport,
            decode_jpeg=False,  # raw bytes mode for tests
        )
        consumer.start()
        try:
            # Wait up to 1 s for the consumer to process both frames.
            deadline = time.time() + 1.0
            while time.time() < deadline:
                frame = consumer.latest_frame
                if frame is not None and frame.capture_ns == 200:
                    break
                time.sleep(0.01)
            assert consumer.latest_frame is not None
            assert consumer.latest_frame.capture_ns == 200
        finally:
            consumer.stop()


class TestMjpegPreviewReconnect:
    def test_on_health_fires_on_stream_error(self):
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            # Return malformed body so the parser raises.
            return httpx.Response(
                200,
                headers={"Content-Type": "multipart/x-mixed-replace; boundary=syncfield"},
                content=b"--syncfield\r\nContent-Type: image/jpeg\r\n\r\n",
            )

        events: list[tuple[str, str]] = []

        consumer = MjpegPreviewConsumer(
            url="http://test/preview/left",
            boundary=b"syncfield",
            transport=httpx.MockTransport(handler),
            decode_jpeg=False,
            on_health=lambda kind, detail: events.append((kind, detail)),
        )
        consumer.start()
        try:
            deadline = time.time() + 1.5
            while time.time() < deadline and not events:
                time.sleep(0.01)
        finally:
            consumer.stop()
        assert events, "expected at least one health event"
        assert events[0][0] == "drop"
