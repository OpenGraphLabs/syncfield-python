"""Unit tests for TimestampTailReader."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from syncfield.adapters.meta_quest_camera.timestamps import TimestampTailReader
from syncfield.types import SampleEvent


def _chunked_jsonl_transport(lines: list[dict]) -> httpx.MockTransport:
    body = b"".join(
        (json.dumps(line) + "\n").encode("ascii") for line in lines
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/x-ndjson"},
            content=body,
        )

    return httpx.MockTransport(handler)


class TestTimestampTailReader:
    def test_emits_sample_event_per_line(self):
        lines = [
            {"frame_number": 0, "capture_ns": 100},
            {"frame_number": 1, "capture_ns": 200},
            {"frame_number": 2, "capture_ns": 300},
        ]
        events: list[SampleEvent] = []

        reader = TimestampTailReader(
            url="http://test/recording/timestamps/left",
            stream_id="quest_cam",
            on_sample=events.append,
            transport=_chunked_jsonl_transport(lines),
            clock_domain="remote_quest3",
            uncertainty_ns=10_000_000,
        )
        reader.start()
        deadline = time.time() + 1.0
        while time.time() < deadline and len(events) < 3:
            time.sleep(0.01)
        reader.stop()

        assert len(events) == 3
        assert [e.frame_number for e in events] == [0, 1, 2]
        assert [e.capture_ns for e in events] == [100, 200, 300]
        assert all(e.clock_domain == "remote_quest3" for e in events)
        assert all(e.uncertainty_ns == 10_000_000 for e in events)
        assert all(e.stream_id == "quest_cam" for e in events)
        assert all(e.channels is None for e in events)

    def test_ignores_malformed_lines(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/x-ndjson"},
                content=(
                    b'{"frame_number": 0, "capture_ns": 1}\n'
                    b"not-json\n"
                    b'{"frame_number": 1, "capture_ns": 2}\n'
                ),
            )

        events: list[SampleEvent] = []
        reader = TimestampTailReader(
            url="http://test/recording/timestamps/left",
            stream_id="quest_cam",
            on_sample=events.append,
            transport=httpx.MockTransport(handler),
        )
        reader.start()
        deadline = time.time() + 1.0
        while time.time() < deadline and len(events) < 2:
            time.sleep(0.01)
        reader.stop()
        assert [e.frame_number for e in events] == [0, 1]
