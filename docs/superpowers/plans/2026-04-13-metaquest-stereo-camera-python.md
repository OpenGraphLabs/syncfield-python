# Meta Quest 3 Stereo Camera Adapter — Python SDK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a `MetaQuestCameraStream` adapter in syncfield-python that pulls a 720p×30 stereo H.264 recording from a Meta Quest 3 running the companion Unity app, streams a low-res MJPEG preview for live viewing, and integrates cleanly with the existing 4-phase `StreamBase` lifecycle.

**Architecture:** One public adapter class (`MetaQuestCameraStream`) composed of small single-purpose collaborators (`QuestHttpClient`, `MjpegPreviewConsumer`, `TimestampTailReader`, `RecordingFilePuller`). Network transport is HTTP on port 14045. All collaborators are tested in isolation with `httpx.MockTransport`; end-to-end integration goes through an in-process `aiohttp` fake Quest server so no real Quest hardware is required in CI.

**Tech Stack:** Python 3.13 · httpx (already a dep) · aiohttp (test-only fake server) · pytest · numpy · OpenCV (for JPEG decoding in preview)

**Scope:** Python SDK only. The Unity-side counterparts (`PassthroughCameraRecorder.cs`, `CameraHttpServer.cs`, `SessionCoordinator.cs`) live in `opengraph-studio/unity` and are tracked in a separate plan. This plan produces a shippable, test-covered Python adapter that can be validated end-to-end against the fake Quest server. Real-hardware QA is a manual checklist at the end.

**Prerequisite (documented, not a Python task):** A Unity-side feasibility probe must confirm that the Quest 3 can sustain hand tracking + 2-camera PCA capture + H.264 hardware encoding + HTTP serving simultaneously at target frame rates. See spec §9 Q1. This plan does not block on it — the Python side can be developed and unit-tested independently — but end-to-end hardware validation at the end of the plan requires the probe to have succeeded.

---

## Spec Reference

See `docs/superpowers/specs/2026-04-13-metaquest-stereo-camera-design.md` for the full design. Key decisions carried into this plan:

- Hybrid recording: MJPEG preview live, 720p H.264 recorded on-device, pulled after `stop_recording()`.
- All network I/O on port 14045 (HTTP).
- `clock_domain="remote_quest3"`, `uncertainty_ns=10_000_000` on every `SampleEvent` and every per-eye timestamps JSONL line.
- Per-eye authoritative timestamp files (`quest_cam_left.timestamps.jsonl`, `quest_cam_right.timestamps.jsonl`) written directly by the adapter (orchestrator's writer is bypassed because it only supports one file per `stream_id`).
- `SampleEvent` stream is driven by left-eye frame arrivals during recording (for live-view liveness + orchestrator sample counts).

---

## File Structure

```
src/syncfield/adapters/meta_quest_camera/
├── __init__.py              # re-exports MetaQuestCameraStream
├── stream.py                # MetaQuestCameraStream(StreamBase) — orchestrates collaborators
├── http_client.py           # QuestHttpClient — typed wrapper over httpx.Client
├── preview.py               # MjpegPreviewConsumer — bg thread that reads /preview/{side}
├── file_puller.py           # RecordingFilePuller — streams MP4 + JSONL to disk
└── timestamps.py            # TimestampTailReader — tails /recording/timestamps/{side}

src/syncfield/adapters/__init__.py
                             # MODIFY: re-export MetaQuestCameraStream (always-on, pure Python)

tests/unit/adapters/meta_quest_camera/
├── __init__.py
├── conftest.py              # shared fixtures (mock transports, sample bytes)
├── test_http_client.py
├── test_preview.py
├── test_file_puller.py
├── test_timestamps.py
└── test_stream_lifecycle.py

tests/helpers/
├── __init__.py              # (create if missing)
└── fake_quest_server.py     # aiohttp app that mimics the Quest's HTTP surface

tests/integration/adapters/
└── test_meta_quest_camera_e2e.py
```

---

## Task Index

| # | Task | Touches |
|---|---|---|
| 1 | Package scaffolding | `src/syncfield/adapters/meta_quest_camera/*` |
| 2 | `QuestHttpClient` — `status()` + base setup | `http_client.py`, `test_http_client.py` |
| 3 | `QuestHttpClient` — `start_recording()` / `stop_recording()` | `http_client.py`, `test_http_client.py` |
| 4 | `QuestHttpClient` — `download_file()` with Range resume | `http_client.py`, `test_http_client.py` |
| 5 | MJPEG multipart parser (pure bytes → frames) | `preview.py`, `test_preview.py` |
| 6 | `MjpegPreviewConsumer` — background reader + `latest_frame` | `preview.py`, `test_preview.py` |
| 7 | `MjpegPreviewConsumer` — reconnect + health events | `preview.py`, `test_preview.py` |
| 8 | `TimestampTailReader` — chunked JSONL → `SampleEvent` | `timestamps.py`, `test_timestamps.py` |
| 9 | `RecordingFilePuller` — download MP4 + JSONL to disk | `file_puller.py`, `test_file_puller.py` |
| 10 | `MetaQuestCameraStream` — identity, capabilities, `device_key` | `stream.py`, `test_stream_lifecycle.py` |
| 11 | `MetaQuestCameraStream.connect()` + `disconnect()` | `stream.py`, `test_stream_lifecycle.py` |
| 12 | `MetaQuestCameraStream.start_recording()` / `stop_recording()` | `stream.py`, `test_stream_lifecycle.py` |
| 13 | `MetaQuestCameraStream.latest_frame_left/right` properties | `stream.py`, `test_stream_lifecycle.py` |
| 14 | Re-export from `adapters/__init__.py` | `adapters/__init__.py` |
| 15 | `FakeQuestServer` test helper | `tests/helpers/fake_quest_server.py` |
| 16 | E2E integration test with orchestrator | `tests/integration/adapters/test_meta_quest_camera_e2e.py` |
| 17 | Manual QA checklist (docs) | `docs/.../hardware-qa-checklist.md` |

---

## Task 1: Package scaffolding

**Files:**
- Create: `src/syncfield/adapters/meta_quest_camera/__init__.py`
- Create: `src/syncfield/adapters/meta_quest_camera/stream.py`
- Create: `src/syncfield/adapters/meta_quest_camera/http_client.py`
- Create: `src/syncfield/adapters/meta_quest_camera/preview.py`
- Create: `src/syncfield/adapters/meta_quest_camera/file_puller.py`
- Create: `src/syncfield/adapters/meta_quest_camera/timestamps.py`
- Create: `tests/unit/adapters/meta_quest_camera/__init__.py`
- Create: `tests/unit/adapters/meta_quest_camera/conftest.py`

- [ ] **Step 1.1: Create package directory and empty modules**

```bash
mkdir -p src/syncfield/adapters/meta_quest_camera
mkdir -p tests/unit/adapters/meta_quest_camera
touch src/syncfield/adapters/meta_quest_camera/{stream,http_client,preview,file_puller,timestamps}.py
touch tests/unit/adapters/meta_quest_camera/__init__.py
```

- [ ] **Step 1.2: Write `meta_quest_camera/__init__.py`**

```python
"""Meta Quest 3 stereo passthrough camera adapter.

Public entry point is :class:`MetaQuestCameraStream`. Internal collaborators
(``QuestHttpClient``, ``MjpegPreviewConsumer``, ``TimestampTailReader``,
``RecordingFilePuller``) live in sibling modules and are composed by the
stream class.
"""

from syncfield.adapters.meta_quest_camera.stream import MetaQuestCameraStream

__all__ = ["MetaQuestCameraStream"]
```

- [ ] **Step 1.3: Write minimal `stream.py` stub so the import line above doesn't fail**

```python
"""MetaQuestCameraStream — top-level adapter class."""

from __future__ import annotations


class MetaQuestCameraStream:
    """Placeholder — filled in by Tasks 10-13."""
```

- [ ] **Step 1.4: Write `tests/unit/adapters/meta_quest_camera/conftest.py`**

```python
"""Shared fixtures for MetaQuestCameraStream unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def quest_host() -> str:
    return "192.0.2.10"  # RFC 5737 TEST-NET-1, never routable


@pytest.fixture
def quest_port() -> int:
    return 14045
```

- [ ] **Step 1.5: Verify import works**

Run: `.venv/bin/python -c "from syncfield.adapters.meta_quest_camera import MetaQuestCameraStream; print('ok')"`
Expected: `ok`

- [ ] **Step 1.6: Commit**

```bash
git add src/syncfield/adapters/meta_quest_camera tests/unit/adapters/meta_quest_camera
git commit -m "chore(meta_quest_camera): scaffold package structure"
```

---

## Task 2: `QuestHttpClient` — `status()` + base setup

**Files:**
- Modify: `src/syncfield/adapters/meta_quest_camera/http_client.py`
- Modify: `tests/unit/adapters/meta_quest_camera/test_http_client.py`

- [ ] **Step 2.1: Write failing test `test_status_returns_parsed_snapshot`**

Create `tests/unit/adapters/meta_quest_camera/test_http_client.py`:

```python
"""Unit tests for QuestHttpClient — no real Quest required."""

from __future__ import annotations

import json

import httpx
import pytest

from syncfield.adapters.meta_quest_camera.http_client import (
    QuestHttpClient,
    QuestStatus,
)


def _mock_transport(handler):
    return httpx.MockTransport(handler)


class TestStatus:
    def test_status_returns_parsed_snapshot(self, quest_host, quest_port):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path == "/status"
            return httpx.Response(
                200,
                json={
                    "recording": False,
                    "session_id": None,
                    "last_preview_capture_ns": 123,
                    "left_camera_ready": True,
                    "right_camera_ready": True,
                    "storage_free_bytes": 42_000_000_000,
                },
            )

        client = QuestHttpClient(
            host=quest_host, port=quest_port, transport=_mock_transport(handler)
        )
        snap = client.status()
        assert isinstance(snap, QuestStatus)
        assert snap.recording is False
        assert snap.left_camera_ready is True
        assert snap.right_camera_ready is True
        assert snap.storage_free_bytes == 42_000_000_000
```

- [ ] **Step 2.2: Run test — verify fails with ImportError**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_http_client.py -x --no-header`
Expected: `ImportError: cannot import name 'QuestHttpClient'`

- [ ] **Step 2.3: Implement `QuestHttpClient` with `status()`**

Write `src/syncfield/adapters/meta_quest_camera/http_client.py`:

```python
"""Typed HTTP client for the Meta Quest 3 companion Unity app.

Wraps :mod:`httpx` with domain-specific request shaping and response
parsing. Accepts a ``transport`` kwarg so unit tests can inject
``httpx.MockTransport`` without spinning up a real server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx


DEFAULT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class QuestStatus:
    """Snapshot of the Quest app's current state (from ``GET /status``)."""

    recording: bool
    session_id: Optional[str]
    last_preview_capture_ns: int
    left_camera_ready: bool
    right_camera_ready: bool
    storage_free_bytes: int

    @classmethod
    def from_json(cls, payload: dict) -> "QuestStatus":
        return cls(
            recording=bool(payload["recording"]),
            session_id=payload.get("session_id"),
            last_preview_capture_ns=int(payload.get("last_preview_capture_ns", 0)),
            left_camera_ready=bool(payload.get("left_camera_ready", False)),
            right_camera_ready=bool(payload.get("right_camera_ready", False)),
            storage_free_bytes=int(payload.get("storage_free_bytes", 0)),
        )


class QuestHttpClient:
    """Thin typed façade over the Quest's HTTP surface (port 14045)."""

    def __init__(
        self,
        host: str,
        port: int = 14045,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base_url = f"http://{host}:{port}"
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "QuestHttpClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def status(self) -> QuestStatus:
        """Fetch a fresh ``QuestStatus`` snapshot from the Quest."""
        response = self._client.get("/status")
        response.raise_for_status()
        return QuestStatus.from_json(response.json())
```

- [ ] **Step 2.4: Run test — verify passes**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_http_client.py -x --no-header`
Expected: `1 passed`

- [ ] **Step 2.5: Commit**

```bash
git add src/syncfield/adapters/meta_quest_camera/http_client.py tests/unit/adapters/meta_quest_camera/test_http_client.py
git commit -m "feat(meta_quest_camera): QuestHttpClient.status() with typed response"
```

---

## Task 3: `QuestHttpClient` — `start_recording()` / `stop_recording()`

**Files:**
- Modify: `src/syncfield/adapters/meta_quest_camera/http_client.py`
- Modify: `tests/unit/adapters/meta_quest_camera/test_http_client.py`

- [ ] **Step 3.1: Write failing tests — `test_start_recording` + `test_stop_recording` + `test_start_recording_409`**

Append to `tests/unit/adapters/meta_quest_camera/test_http_client.py`:

```python
from syncfield.adapters.meta_quest_camera.http_client import (
    RecordingStartResponse,
    RecordingStopResponse,
    RecordingAlreadyActive,
)


class TestStartRecording:
    def test_start_recording_happy_path(self, quest_host, quest_port):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/recording/start"
            body = json.loads(request.content)
            assert body["session_id"] == "ep_x"
            assert body["host_mono_ns"] == 111
            assert body["resolution"] == {"width": 1280, "height": 720}
            assert body["fps"] == 30
            return httpx.Response(
                200,
                json={
                    "session_id": "ep_x",
                    "quest_mono_ns_at_start": 42,
                    "delta_ns": 69,
                    "started": True,
                },
            )

        client = QuestHttpClient(
            host=quest_host, port=quest_port, transport=_mock_transport(handler)
        )
        res = client.start_recording(
            session_id="ep_x", host_mono_ns=111, width=1280, height=720, fps=30
        )
        assert isinstance(res, RecordingStartResponse)
        assert res.session_id == "ep_x"
        assert res.delta_ns == 69
        assert res.started is True

    def test_start_recording_409_raises(self, quest_host, quest_port):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(409, json={"error": "session_already_active"})

        client = QuestHttpClient(
            host=quest_host, port=quest_port, transport=_mock_transport(handler)
        )
        with pytest.raises(RecordingAlreadyActive):
            client.start_recording(
                session_id="ep_x", host_mono_ns=1, width=1280, height=720, fps=30
            )


class TestStopRecording:
    def test_stop_recording_happy_path(self, quest_host, quest_port):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/recording/stop"
            return httpx.Response(
                200,
                json={
                    "session_id": "ep_x",
                    "left":  {"frame_count": 100, "bytes": 1000, "last_capture_ns": 9},
                    "right": {"frame_count": 100, "bytes": 1001, "last_capture_ns": 9},
                    "duration_s": 3.33,
                },
            )

        client = QuestHttpClient(
            host=quest_host, port=quest_port, transport=_mock_transport(handler)
        )
        res = client.stop_recording()
        assert isinstance(res, RecordingStopResponse)
        assert res.left.frame_count == 100
        assert res.right.frame_count == 100
        assert res.duration_s == pytest.approx(3.33)
```

- [ ] **Step 3.2: Run tests — verify they fail with ImportError**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_http_client.py -x --no-header`
Expected: `ImportError: cannot import name 'RecordingStartResponse'`

- [ ] **Step 3.3: Implement the new types + methods**

Append to `src/syncfield/adapters/meta_quest_camera/http_client.py`:

```python
class RecordingAlreadyActive(RuntimeError):
    """Raised when POST /recording/start returns 409."""


@dataclass(frozen=True)
class RecordingStartResponse:
    session_id: str
    quest_mono_ns_at_start: int
    delta_ns: int
    started: bool

    @classmethod
    def from_json(cls, payload: dict) -> "RecordingStartResponse":
        return cls(
            session_id=str(payload["session_id"]),
            quest_mono_ns_at_start=int(payload["quest_mono_ns_at_start"]),
            delta_ns=int(payload["delta_ns"]),
            started=bool(payload["started"]),
        )


@dataclass(frozen=True)
class PerEyeSummary:
    frame_count: int
    bytes: int
    last_capture_ns: int

    @classmethod
    def from_json(cls, payload: dict) -> "PerEyeSummary":
        return cls(
            frame_count=int(payload["frame_count"]),
            bytes=int(payload["bytes"]),
            last_capture_ns=int(payload["last_capture_ns"]),
        )


@dataclass(frozen=True)
class RecordingStopResponse:
    session_id: str
    left: PerEyeSummary
    right: PerEyeSummary
    duration_s: float

    @classmethod
    def from_json(cls, payload: dict) -> "RecordingStopResponse":
        return cls(
            session_id=str(payload["session_id"]),
            left=PerEyeSummary.from_json(payload["left"]),
            right=PerEyeSummary.from_json(payload["right"]),
            duration_s=float(payload["duration_s"]),
        )
```

Then add methods to `QuestHttpClient`:

```python
    def start_recording(
        self,
        *,
        session_id: str,
        host_mono_ns: int,
        width: int,
        height: int,
        fps: int,
    ) -> RecordingStartResponse:
        response = self._client.post(
            "/recording/start",
            json={
                "session_id": session_id,
                "host_mono_ns": host_mono_ns,
                "resolution": {"width": width, "height": height},
                "fps": fps,
            },
        )
        if response.status_code == 409:
            raise RecordingAlreadyActive(response.json().get("error", ""))
        response.raise_for_status()
        return RecordingStartResponse.from_json(response.json())

    def stop_recording(self) -> RecordingStopResponse:
        response = self._client.post("/recording/stop", json={})
        response.raise_for_status()
        return RecordingStopResponse.from_json(response.json())
```

- [ ] **Step 3.4: Run tests — verify all pass**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_http_client.py -v --no-header`
Expected: `4 passed`

- [ ] **Step 3.5: Commit**

```bash
git add src/syncfield/adapters/meta_quest_camera/http_client.py tests/unit/adapters/meta_quest_camera/test_http_client.py
git commit -m "feat(meta_quest_camera): QuestHttpClient start/stop recording with 409 handling"
```

---

## Task 4: `QuestHttpClient` — `download_file()` with Range resume

**Files:**
- Modify: `src/syncfield/adapters/meta_quest_camera/http_client.py`
- Modify: `tests/unit/adapters/meta_quest_camera/test_http_client.py`

- [ ] **Step 4.1: Write failing tests — `test_download_file` + `test_download_file_resumes_on_failure`**

Append to `tests/unit/adapters/meta_quest_camera/test_http_client.py`:

```python
class TestDownload:
    def test_download_file_writes_all_bytes(self, quest_host, quest_port, tmp_path):
        payload = b"\x00\x01\x02" * 1024  # 3 KiB

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path == "/recording/files/left"
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(payload))},
                content=payload,
            )

        client = QuestHttpClient(
            host=quest_host, port=quest_port, transport=_mock_transport(handler)
        )
        dest = tmp_path / "left.mp4"
        bytes_written = client.download_file("/recording/files/left", dest)
        assert bytes_written == len(payload)
        assert dest.read_bytes() == payload

    def test_download_file_resumes_with_range(self, quest_host, quest_port, tmp_path):
        payload = b"A" * 100 + b"B" * 100  # 200 bytes total
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                # First attempt: send half, then close (simulate drop).
                return httpx.Response(
                    200,
                    headers={"Content-Length": "200"},
                    content=payload[:100],
                )
            # Second attempt: honor the Range header.
            range_hdr = request.headers["Range"]
            assert range_hdr == "bytes=100-"
            return httpx.Response(
                206,
                headers={"Content-Length": "100", "Content-Range": "bytes 100-199/200"},
                content=payload[100:],
            )

        client = QuestHttpClient(
            host=quest_host, port=quest_port, transport=_mock_transport(handler)
        )
        dest = tmp_path / "left.mp4"
        total = client.download_file("/recording/files/left", dest, max_retries=3)
        assert total == 200
        assert dest.read_bytes() == payload
```

- [ ] **Step 4.2: Run tests — verify first fails with AttributeError on `download_file`**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_http_client.py::TestDownload -x --no-header`
Expected: `AttributeError: 'QuestHttpClient' object has no attribute 'download_file'`

- [ ] **Step 4.3: Implement `download_file` with Range-based resume**

Add to `QuestHttpClient` in `http_client.py`:

```python
    def download_file(
        self,
        path: str,
        dest,
        *,
        max_retries: int = 3,
        chunk_size: int = 64 * 1024,
    ) -> int:
        """Download a binary resource, streaming to ``dest`` on disk.

        Resumes with ``Range`` headers on transient failure up to
        ``max_retries`` times. Returns the total number of bytes written.
        The caller is responsible for verifying the expected size against
        the value returned by ``/recording/stop``.
        """
        from pathlib import Path

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        written = 0
        expected_total: Optional[int] = None

        for attempt in range(max_retries):
            headers = {}
            mode = "wb"
            if written > 0:
                headers["Range"] = f"bytes={written}-"
                mode = "ab"

            try:
                with self._client.stream("GET", path, headers=headers) as response:
                    response.raise_for_status()
                    if expected_total is None:
                        cl = response.headers.get("Content-Length")
                        cr = response.headers.get("Content-Range")
                        if cr and "/" in cr:
                            expected_total = int(cr.rsplit("/", 1)[1])
                        elif cl is not None and written == 0:
                            expected_total = int(cl)
                    with open(dest, mode) as fh:
                        for chunk in response.iter_bytes(chunk_size):
                            fh.write(chunk)
                            written += len(chunk)
                if expected_total is None or written >= expected_total:
                    return written
            except httpx.HTTPError:
                if attempt == max_retries - 1:
                    raise
                continue

        return written
```

- [ ] **Step 4.4: Run tests — verify all pass**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_http_client.py -v --no-header`
Expected: `6 passed`

- [ ] **Step 4.5: Commit**

```bash
git add src/syncfield/adapters/meta_quest_camera/http_client.py tests/unit/adapters/meta_quest_camera/test_http_client.py
git commit -m "feat(meta_quest_camera): QuestHttpClient.download_file with Range resume"
```

---

## Task 5: MJPEG multipart parser (pure bytes → frames)

Pure function — no threads, no HTTP. Lets us test parsing in isolation from network behaviour.

**Files:**
- Modify: `src/syncfield/adapters/meta_quest_camera/preview.py`
- Modify: `tests/unit/adapters/meta_quest_camera/test_preview.py` (create)

- [ ] **Step 5.1: Write failing tests**

Create `tests/unit/adapters/meta_quest_camera/test_preview.py`:

```python
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
```

- [ ] **Step 5.2: Run test — verify fails with ImportError**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_preview.py -x --no-header`
Expected: `ImportError: cannot import name 'MjpegFrame'`

- [ ] **Step 5.3: Implement the parser**

Write `src/syncfield/adapters/meta_quest_camera/preview.py`:

```python
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
        line = _readline(stream).rstrip(b"\r\n")
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
```

- [ ] **Step 5.4: Run tests — verify passes**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_preview.py -v --no-header`
Expected: `3 passed`

- [ ] **Step 5.5: Commit**

```bash
git add src/syncfield/adapters/meta_quest_camera/preview.py tests/unit/adapters/meta_quest_camera/test_preview.py
git commit -m "feat(meta_quest_camera): strict MJPEG multipart parser"
```

---

## Task 6: `MjpegPreviewConsumer` — background reader + `latest_frame`

**Files:**
- Modify: `src/syncfield/adapters/meta_quest_camera/preview.py`
- Modify: `tests/unit/adapters/meta_quest_camera/test_preview.py`

- [ ] **Step 6.1: Write failing test**

Append to `tests/unit/adapters/meta_quest_camera/test_preview.py`:

```python
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
```

- [ ] **Step 6.2: Run test — verify fails with ImportError**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_preview.py::TestMjpegPreviewConsumer -x --no-header`
Expected: `ImportError: cannot import name 'MjpegPreviewConsumer'`

- [ ] **Step 6.3: Implement `MjpegPreviewConsumer`**

Append to `src/syncfield/adapters/meta_quest_camera/preview.py`:

```python
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
                buffer = _StreamAdapter(response.iter_raw(8192), self._stop_event)
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
    """Decode JPEG bytes to a BGR ``numpy.ndarray`` via OpenCV.

    Imported lazily so the adapter module stays importable on hosts that
    don't have OpenCV installed (tests pass ``decode_jpeg=False``).
    """
    import cv2
    import numpy as np

    buf = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)
```

- [ ] **Step 6.4: Run test — verify passes**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_preview.py -v --no-header`
Expected: `4 passed` (3 parser + 1 consumer)

- [ ] **Step 6.5: Commit**

```bash
git add src/syncfield/adapters/meta_quest_camera/preview.py tests/unit/adapters/meta_quest_camera/test_preview.py
git commit -m "feat(meta_quest_camera): MjpegPreviewConsumer background reader"
```

---

## Task 7: `MjpegPreviewConsumer` — reconnect + health callback

**Files:**
- Modify: `tests/unit/adapters/meta_quest_camera/test_preview.py`

The reconnect path is already in the `_run` loop from Task 6. This task only adds a test that verifies the `on_health` callback fires.

- [ ] **Step 7.1: Write failing test `test_on_health_fires_on_error`**

Append to `tests/unit/adapters/meta_quest_camera/test_preview.py`:

```python
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
```

- [ ] **Step 7.2: Run test — verify passes immediately (logic implemented in Task 6)**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_preview.py::TestMjpegPreviewReconnect -v --no-header`
Expected: `1 passed`

- [ ] **Step 7.3: Commit**

```bash
git add tests/unit/adapters/meta_quest_camera/test_preview.py
git commit -m "test(meta_quest_camera): verify on_health fires on MJPEG stream error"
```

---

## Task 8: `TimestampTailReader` — chunked JSONL → `SampleEvent`

**Files:**
- Modify: `src/syncfield/adapters/meta_quest_camera/timestamps.py`
- Modify: `tests/unit/adapters/meta_quest_camera/test_timestamps.py` (create)

- [ ] **Step 8.1: Write failing tests**

Create `tests/unit/adapters/meta_quest_camera/test_timestamps.py`:

```python
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
```

- [ ] **Step 8.2: Run test — verify fails with ImportError**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_timestamps.py -x --no-header`
Expected: `ImportError: cannot import name 'TimestampTailReader'`

- [ ] **Step 8.3: Implement `TimestampTailReader`**

Write `src/syncfield/adapters/meta_quest_camera/timestamps.py`:

```python
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
```

- [ ] **Step 8.4: Run tests — verify passes**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_timestamps.py -v --no-header`
Expected: `2 passed`

- [ ] **Step 8.5: Commit**

```bash
git add src/syncfield/adapters/meta_quest_camera/timestamps.py tests/unit/adapters/meta_quest_camera/test_timestamps.py
git commit -m "feat(meta_quest_camera): TimestampTailReader emits SampleEvent per JSONL line"
```

---

## Task 9: `RecordingFilePuller` — download MP4 + JSONL to disk

A thin orchestration layer over `QuestHttpClient.download_file` that hides the per-eye + per-kind URL construction from the adapter class.

**Files:**
- Modify: `src/syncfield/adapters/meta_quest_camera/file_puller.py`
- Modify: `tests/unit/adapters/meta_quest_camera/test_file_puller.py` (create)

- [ ] **Step 9.1: Write failing tests**

Create `tests/unit/adapters/meta_quest_camera/test_file_puller.py`:

```python
"""Unit tests for RecordingFilePuller."""

from __future__ import annotations

import httpx
import pytest

from syncfield.adapters.meta_quest_camera.file_puller import (
    RecordingFilePuller,
    RecordingArtifacts,
)
from syncfield.adapters.meta_quest_camera.http_client import QuestHttpClient


def _router():
    files = {
        "/recording/files/left": b"LEFT_MP4",
        "/recording/files/right": b"RIGHT_MP4",
        "/recording/timestamps/left":
            b'{"frame_number":0,"capture_ns":1}\n{"frame_number":1,"capture_ns":2}\n',
        "/recording/timestamps/right":
            b'{"frame_number":0,"capture_ns":1}\n{"frame_number":1,"capture_ns":2}\n',
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = files.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"Content-Length": str(len(body))}, content=body
        )

    return httpx.MockTransport(handler)


class TestRecordingFilePuller:
    def test_pulls_all_four_artifacts(self, tmp_path):
        client = QuestHttpClient(host="test", port=14045, transport=_router())
        puller = RecordingFilePuller(
            client=client, stream_id="quest_cam", output_dir=tmp_path
        )
        artifacts = puller.pull_all()

        assert isinstance(artifacts, RecordingArtifacts)
        assert artifacts.left_mp4.read_bytes() == b"LEFT_MP4"
        assert artifacts.right_mp4.read_bytes() == b"RIGHT_MP4"
        assert artifacts.left_timestamps.exists()
        assert artifacts.right_timestamps.exists()

        # File naming matches the adapter's documented output layout.
        assert artifacts.left_mp4.name == "quest_cam_left.mp4"
        assert artifacts.right_mp4.name == "quest_cam_right.mp4"
        assert artifacts.left_timestamps.name == "quest_cam_left.timestamps.jsonl"
        assert artifacts.right_timestamps.name == "quest_cam_right.timestamps.jsonl"
```

- [ ] **Step 9.2: Run test — verify fails with ImportError**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_file_puller.py -x --no-header`
Expected: `ImportError: cannot import name 'RecordingFilePuller'`

- [ ] **Step 9.3: Implement `RecordingFilePuller`**

Write `src/syncfield/adapters/meta_quest_camera/file_puller.py`:

```python
"""Pulls the four per-session artifacts (2 MP4s + 2 timestamps JSONLs) from
the Quest's HTTP surface into the SyncField session output directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from syncfield.adapters.meta_quest_camera.http_client import QuestHttpClient


@dataclass(frozen=True)
class RecordingArtifacts:
    """Paths written to ``output_dir`` by a successful ``pull_all``."""

    left_mp4: Path
    right_mp4: Path
    left_timestamps: Path
    right_timestamps: Path


class RecordingFilePuller:
    """Downloads all per-session artifacts into ``output_dir``.

    File naming mirrors the adapter's public contract:

    - ``{stream_id}_{side}.mp4``
    - ``{stream_id}_{side}.timestamps.jsonl``
    """

    def __init__(
        self,
        *,
        client: QuestHttpClient,
        stream_id: str,
        output_dir: Path,
    ) -> None:
        self._client = client
        self._stream_id = stream_id
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def pull_all(self) -> RecordingArtifacts:
        prefix = self._stream_id
        paths = RecordingArtifacts(
            left_mp4=self._output_dir / f"{prefix}_left.mp4",
            right_mp4=self._output_dir / f"{prefix}_right.mp4",
            left_timestamps=self._output_dir / f"{prefix}_left.timestamps.jsonl",
            right_timestamps=self._output_dir / f"{prefix}_right.timestamps.jsonl",
        )
        self._client.download_file("/recording/files/left", paths.left_mp4)
        self._client.download_file("/recording/files/right", paths.right_mp4)
        self._client.download_file(
            "/recording/timestamps/left", paths.left_timestamps
        )
        self._client.download_file(
            "/recording/timestamps/right", paths.right_timestamps
        )
        return paths
```

- [ ] **Step 9.4: Run tests — verify passes**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_file_puller.py -v --no-header`
Expected: `1 passed`

- [ ] **Step 9.5: Commit**

```bash
git add src/syncfield/adapters/meta_quest_camera/file_puller.py tests/unit/adapters/meta_quest_camera/test_file_puller.py
git commit -m "feat(meta_quest_camera): RecordingFilePuller pulls MP4s + JSONLs to output_dir"
```

---

## Task 10: `MetaQuestCameraStream` — identity, capabilities, `device_key`

**Files:**
- Modify: `src/syncfield/adapters/meta_quest_camera/stream.py`
- Modify: `tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py` (create)

- [ ] **Step 10.1: Write failing tests**

Create `tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py`:

```python
"""Unit tests for the top-level MetaQuestCameraStream adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncfield.adapters.meta_quest_camera import MetaQuestCameraStream


class TestIdentity:
    def test_stream_identity_and_capabilities(self, tmp_path: Path):
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="192.0.2.10",
            output_dir=tmp_path,
        )
        assert stream.id == "quest_cam"
        assert stream.kind == "video"
        assert stream.capabilities.produces_file is True
        assert stream.capabilities.supports_precise_timestamps is True
        assert stream.capabilities.is_removable is True
        assert stream.capabilities.provides_audio_track is False

    def test_device_key_includes_host(self, tmp_path: Path):
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="192.0.2.10",
            output_dir=tmp_path,
        )
        assert stream.device_key == ("meta_quest_camera", "192.0.2.10")
```

- [ ] **Step 10.2: Run tests — verify fail**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py -x --no-header`
Expected: failures (class is still the Task-1 placeholder)

- [ ] **Step 10.3: Implement identity + capabilities in `stream.py`**

Replace `src/syncfield/adapters/meta_quest_camera/stream.py`:

```python
"""MetaQuestCameraStream — SyncField adapter for Quest 3 stereo passthrough cameras."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

from syncfield.stream import DeviceKey, StreamBase
from syncfield.types import StreamCapabilities


logger = logging.getLogger(__name__)


# Matches the Quest companion Unity app's default HTTP port (spec §2).
DEFAULT_QUEST_HTTP_PORT = 14045
DEFAULT_FPS = 30
DEFAULT_RESOLUTION: Tuple[int, int] = (1280, 720)


class MetaQuestCameraStream(StreamBase):
    """Captures Meta Quest 3 stereo passthrough cameras (hybrid mode).

    Live: low-res MJPEG preview pulled from the Quest for the viewer.
    Recorded: 720p×30 H.264 recorded on the Quest, pulled to
    ``output_dir`` after :meth:`stop_recording` completes.

    See ``docs/superpowers/specs/2026-04-13-metaquest-stereo-camera-design.md``
    for the full protocol + architecture notes.
    """

    CLOCK_DOMAIN = "remote_quest3"
    UNCERTAINTY_NS = 10_000_000  # 10 ms — WiFi jitter budget, matches MetaQuestHandStream

    def __init__(
        self,
        id: str,
        *,
        quest_host: str,
        output_dir: Path,
        quest_port: int = DEFAULT_QUEST_HTTP_PORT,
        fps: int = DEFAULT_FPS,
        resolution: Tuple[int, int] = DEFAULT_RESOLUTION,
    ) -> None:
        super().__init__(
            id=id,
            kind="video",
            capabilities=StreamCapabilities(
                provides_audio_track=False,
                supports_precise_timestamps=True,
                is_removable=True,
                produces_file=True,
            ),
        )
        self._quest_host = quest_host
        self._quest_port = quest_port
        self._fps = fps
        self._resolution = resolution
        self._output_dir = Path(output_dir)

    @property
    def device_key(self) -> Optional[DeviceKey]:
        return ("meta_quest_camera", self._quest_host)
```

- [ ] **Step 10.4: Run tests — verify pass**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py -v --no-header`
Expected: `2 passed`

- [ ] **Step 10.5: Commit**

```bash
git add src/syncfield/adapters/meta_quest_camera/stream.py tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py
git commit -m "feat(meta_quest_camera): MetaQuestCameraStream identity + capabilities + device_key"
```

---

## Task 11: `MetaQuestCameraStream.connect()` + `disconnect()`

`connect()` does a `GET /status` health check, then starts the two MJPEG preview consumers so live view flows while the session is `CONNECTED`. `disconnect()` stops the consumers.

**Files:**
- Modify: `src/syncfield/adapters/meta_quest_camera/stream.py`
- Modify: `tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py`

- [ ] **Step 11.1: Write failing tests**

Append to `tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py`:

```python
import httpx


def _status_only_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(
                200,
                json={
                    "recording": False,
                    "session_id": None,
                    "last_preview_capture_ns": 0,
                    "left_camera_ready": True,
                    "right_camera_ready": True,
                    "storage_free_bytes": 1_000_000_000,
                },
            )
        if request.url.path.startswith("/preview/"):
            # Return a tiny valid MJPEG body (no frames) so the consumer blocks.
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "multipart/x-mixed-replace; boundary=syncfield"
                },
                content=b"",
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class TestConnectDisconnect:
    def test_connect_runs_status_probe_and_starts_preview(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="test",
            output_dir=tmp_path,
            _transport=_status_only_transport(),  # test-only injection
        )
        stream.connect()
        assert stream.is_connected is True
        stream.disconnect()
        assert stream.is_connected is False

    def test_connect_raises_when_quest_unreachable(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable")

        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="test",
            output_dir=tmp_path,
            _transport=httpx.MockTransport(handler),
        )
        with pytest.raises(httpx.ConnectError):
            stream.connect()
```

- [ ] **Step 11.2: Run tests — verify fail**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py::TestConnectDisconnect -x --no-header`
Expected: `TypeError: unexpected keyword argument '_transport'`

- [ ] **Step 11.3: Implement `connect()` / `disconnect()` + `is_connected` + test transport injection**

Update `src/syncfield/adapters/meta_quest_camera/stream.py`:

Add these imports at top:

```python
import httpx

from syncfield.adapters.meta_quest_camera.http_client import QuestHttpClient
from syncfield.adapters.meta_quest_camera.preview import MjpegPreviewConsumer
from syncfield.types import HealthEvent, HealthEventKind
import time
```

Extend `__init__` parameters (add `_transport` keyword after existing ones; keep underscore-prefix to signal "test-only seam"):

```python
        _transport: Optional[httpx.BaseTransport] = None,
```

Store it:

```python
        self._transport = _transport
        self._http: Optional[QuestHttpClient] = None
        self._preview_left: Optional[MjpegPreviewConsumer] = None
        self._preview_right: Optional[MjpegPreviewConsumer] = None
        self._connected = False
```

Add methods:

```python
    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        if self._connected:
            return
        self._http = QuestHttpClient(
            host=self._quest_host,
            port=self._quest_port,
            transport=self._transport,
        )
        # Probe reachability up front so failures surface before recording starts.
        self._http.status()
        self._preview_left = self._make_preview("left")
        self._preview_right = self._make_preview("right")
        self._preview_left.start()
        self._preview_right.start()
        self._connected = True
        logger.info(
            "[%s] connected to Quest %s:%d",
            self.id, self._quest_host, self._quest_port,
        )

    def disconnect(self) -> None:
        if self._preview_left is not None:
            self._preview_left.stop()
            self._preview_left = None
        if self._preview_right is not None:
            self._preview_right.stop()
            self._preview_right = None
        if self._http is not None:
            self._http.close()
            self._http = None
        self._connected = False

    # ------------------------------------------------------------------

    def _make_preview(self, side: str) -> MjpegPreviewConsumer:
        url = f"http://{self._quest_host}:{self._quest_port}/preview/{side}"

        def _on_health(kind: str, detail: str) -> None:
            mapping = {
                "drop": HealthEventKind.DROP,
                "reconnect": HealthEventKind.RECONNECT,
                "warning": HealthEventKind.WARNING,
            }
            self._emit_health(
                HealthEvent(
                    stream_id=self.id,
                    kind=mapping.get(kind, HealthEventKind.WARNING),
                    at_ns=time.monotonic_ns(),
                    detail=f"[{side}] {detail}",
                )
            )

        return MjpegPreviewConsumer(
            url=url,
            boundary=b"syncfield",
            transport=self._transport,
            decode_jpeg=True,
            on_health=_on_health,
        )
```

- [ ] **Step 11.4: Run tests — verify pass**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py -v --no-header`
Expected: `4 passed`

- [ ] **Step 11.5: Commit**

```bash
git add src/syncfield/adapters/meta_quest_camera/stream.py tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py
git commit -m "feat(meta_quest_camera): connect()/disconnect() with health-probed preview start"
```

---

## Task 12: `MetaQuestCameraStream.start_recording()` / `stop_recording()`

Wires everything together: POST /start → spawn TimestampTailReader → on stop_recording() POST /stop + pull all artifacts via RecordingFilePuller → return FinalizationReport.

**Files:**
- Modify: `src/syncfield/adapters/meta_quest_camera/stream.py`
- Modify: `tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py`

- [ ] **Step 12.1: Write failing test `test_full_recording_roundtrip`**

Append to `tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py`:

```python
import json
from syncfield.clock import SessionClock, SyncPoint


def _full_quest_transport(left_mp4=b"LEFT_MP4", right_mp4=b"RIGHT_MP4"):
    state = {"recording": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/status":
            return httpx.Response(200, json={
                "recording": state["recording"], "session_id": None,
                "last_preview_capture_ns": 0,
                "left_camera_ready": True, "right_camera_ready": True,
                "storage_free_bytes": 1_000_000_000,
            })
        if path.startswith("/preview/"):
            return httpx.Response(200, headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=syncfield"
            }, content=b"")
        if path == "/recording/start":
            state["recording"] = True
            return httpx.Response(200, json={
                "session_id": "ep_x", "quest_mono_ns_at_start": 0,
                "delta_ns": 0, "started": True,
            })
        if path == "/recording/stop":
            state["recording"] = False
            return httpx.Response(200, json={
                "session_id": "ep_x",
                "left":  {"frame_count": 2, "bytes": len(left_mp4),  "last_capture_ns": 2},
                "right": {"frame_count": 2, "bytes": len(right_mp4), "last_capture_ns": 2},
                "duration_s": 0.1,
            })
        if path == "/recording/files/left":
            return httpx.Response(200, headers={"Content-Length": str(len(left_mp4))}, content=left_mp4)
        if path == "/recording/files/right":
            return httpx.Response(200, headers={"Content-Length": str(len(right_mp4))}, content=right_mp4)
        if path == "/recording/timestamps/left" or path == "/recording/timestamps/right":
            body = (
                b'{"frame_number":0,"capture_ns":1}\n'
                b'{"frame_number":1,"capture_ns":2}\n'
            )
            return httpx.Response(200, headers={"Content-Length": str(len(body))}, content=body)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class TestRecordingRoundtrip:
    def test_full_recording_lifecycle(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host="test",
            output_dir=tmp_path,
            _transport=_full_quest_transport(),
        )
        stream.connect()
        clock = SessionClock(sync_point=SyncPoint.create_now("test_host"))

        stream.start_recording(clock)
        report = stream.stop_recording()
        stream.disconnect()

        assert report.status == "completed"
        assert (tmp_path / "quest_cam_left.mp4").read_bytes() == b"LEFT_MP4"
        assert (tmp_path / "quest_cam_right.mp4").read_bytes() == b"RIGHT_MP4"
        assert (tmp_path / "quest_cam_left.timestamps.jsonl").exists()
        assert (tmp_path / "quest_cam_right.timestamps.jsonl").exists()
```

- [ ] **Step 12.2: Run test — verify fails**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py::TestRecordingRoundtrip -x --no-header`
Expected: `NotImplementedError: start_recording` (or AttributeError on one of the new methods)

- [ ] **Step 12.3: Implement `start_recording()` + `stop_recording()` + `prepare()` no-op**

Append to `src/syncfield/adapters/meta_quest_camera/stream.py`:

Add imports:

```python
from syncfield.adapters.meta_quest_camera.file_puller import RecordingFilePuller
from syncfield.adapters.meta_quest_camera.timestamps import TimestampTailReader
from syncfield.clock import SessionClock
from syncfield.types import FinalizationReport
```

Add state in `__init__`:

```python
        self._timestamp_tail: Optional[TimestampTailReader] = None
        self._session_id: Optional[str] = None
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None
        self._frame_count = 0
```

Add methods:

```python
    def prepare(self) -> None:
        pass

    def start_recording(self, session_clock: SessionClock) -> None:
        if self._http is None:
            raise RuntimeError("start_recording() called before connect()")

        self._session_id = f"ep_{int(time.monotonic_ns())}"
        self._frame_count = 0
        self._first_at = None
        self._last_at = None

        self._http.start_recording(
            session_id=self._session_id,
            host_mono_ns=session_clock.sync_point.monotonic_ns,
            width=self._resolution[0],
            height=self._resolution[1],
            fps=self._fps,
        )

        # Tail the LEFT eye's chunked timestamps endpoint; right eye's exact
        # per-frame ts lives in the authoritative JSONL written by the puller.
        url = (
            f"http://{self._quest_host}:{self._quest_port}"
            f"/recording/timestamps/left"
        )
        self._timestamp_tail = TimestampTailReader(
            url=url,
            stream_id=self.id,
            on_sample=self._handle_tail_sample,
            transport=self._transport,
            clock_domain=self.CLOCK_DOMAIN,
            uncertainty_ns=self.UNCERTAINTY_NS,
        )
        self._timestamp_tail.start()

    def stop_recording(self) -> FinalizationReport:
        if self._http is None:
            raise RuntimeError("stop_recording() called before connect()")

        try:
            self._http.stop_recording()
            if self._timestamp_tail is not None:
                self._timestamp_tail.stop()
                self._timestamp_tail = None

            puller = RecordingFilePuller(
                client=self._http, stream_id=self.id, output_dir=self._output_dir
            )
            artifacts = puller.pull_all()
            status = "completed"
            error: Optional[str] = None
        except Exception as exc:
            status = "failed"
            error = str(exc)
            artifacts = None

        return FinalizationReport(
            stream_id=self.id,
            status=status,
            frame_count=self._frame_count,
            file_path=artifacts.left_mp4 if artifacts is not None else None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=error,
        )

    def _handle_tail_sample(self, event) -> None:
        if self._first_at is None:
            self._first_at = event.capture_ns
        self._last_at = event.capture_ns
        self._frame_count += 1
        self._emit_sample(event)
```

- [ ] **Step 12.4: Run tests — verify pass**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py -v --no-header`
Expected: `5 passed`

- [ ] **Step 12.5: Commit**

```bash
git add src/syncfield/adapters/meta_quest_camera/stream.py tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py
git commit -m "feat(meta_quest_camera): end-to-end start/stop recording with file pull"
```

---

## Task 13: `latest_frame_left` / `latest_frame_right` properties

**Files:**
- Modify: `src/syncfield/adapters/meta_quest_camera/stream.py`
- Modify: `tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py`

- [ ] **Step 13.1: Write failing test**

Append to `tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py`:

```python
class TestLatestFrame:
    def test_latest_frame_none_before_connect(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam", quest_host="test", output_dir=tmp_path,
        )
        assert stream.latest_frame_left is None
        assert stream.latest_frame_right is None

    def test_latest_frame_reads_from_preview_consumers(self, tmp_path):
        stream = MetaQuestCameraStream(
            id="quest_cam", quest_host="test", output_dir=tmp_path,
            _transport=_status_only_transport(),
        )
        stream.connect()
        # Consumers returned empty body in the fixture, so latest_frame stays None.
        assert stream.latest_frame_left is None
        assert stream.latest_frame_right is None
        stream.disconnect()
```

- [ ] **Step 13.2: Run tests — verify fail with AttributeError**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py::TestLatestFrame -x --no-header`
Expected: `AttributeError: 'MetaQuestCameraStream' object has no attribute 'latest_frame_left'`

- [ ] **Step 13.3: Add properties**

Append to `src/syncfield/adapters/meta_quest_camera/stream.py`:

```python
    @property
    def latest_frame_left(self):
        """Most-recent decoded BGR preview frame from the left camera, or None."""
        if self._preview_left is None:
            return None
        return self._preview_left.latest_frame

    @property
    def latest_frame_right(self):
        if self._preview_right is None:
            return None
        return self._preview_right.latest_frame
```

- [ ] **Step 13.4: Run tests — verify pass**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py -v --no-header`
Expected: `7 passed`

- [ ] **Step 13.5: Commit**

```bash
git add src/syncfield/adapters/meta_quest_camera/stream.py tests/unit/adapters/meta_quest_camera/test_stream_lifecycle.py
git commit -m "feat(meta_quest_camera): latest_frame_left/right pass through preview consumers"
```

---

## Task 14: Re-export from `adapters/__init__.py`

Make the adapter discoverable via `from syncfield.adapters import MetaQuestCameraStream`.

**Files:**
- Modify: `src/syncfield/adapters/__init__.py`

- [ ] **Step 14.1: Write failing test**

Create `tests/unit/adapters/meta_quest_camera/test_public_reexport.py`:

```python
"""Smoke test: the adapter is visible from the top-level adapters module."""


def test_reexport_top_level():
    from syncfield.adapters import MetaQuestCameraStream
    assert MetaQuestCameraStream is not None

def test_listed_in_all():
    import syncfield.adapters as adapters
    assert "MetaQuestCameraStream" in adapters.__all__
```

- [ ] **Step 14.2: Run tests — verify fail**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera/test_public_reexport.py -x --no-header`
Expected: `ImportError: cannot import name 'MetaQuestCameraStream'`

- [ ] **Step 14.3: Add re-export to `src/syncfield/adapters/__init__.py`**

Modify the top-of-file import block. Change:

```python
from syncfield.adapters.jsonl_file import JSONLFileStream
from syncfield.adapters.meta_quest import MetaQuestHandStream
from syncfield.adapters.polling_sensor import PollingSensorStream
from syncfield.adapters.push_sensor import PushSensorStream
```

to:

```python
from syncfield.adapters.jsonl_file import JSONLFileStream
from syncfield.adapters.meta_quest import MetaQuestHandStream
from syncfield.adapters.meta_quest_camera import MetaQuestCameraStream
from syncfield.adapters.polling_sensor import PollingSensorStream
from syncfield.adapters.push_sensor import PushSensorStream
```

And update `__all__`:

```python
__all__ = [
    "JSONLFileStream",
    "MetaQuestCameraStream",
    "MetaQuestHandStream",
    "PollingSensorStream",
    "PushSensorStream",
]
```

- [ ] **Step 14.4: Run tests — verify pass**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera -v --no-header`
Expected: all previous tests + 2 new ones pass

- [ ] **Step 14.5: Commit**

```bash
git add src/syncfield/adapters/__init__.py tests/unit/adapters/meta_quest_camera/test_public_reexport.py
git commit -m "feat(meta_quest_camera): re-export MetaQuestCameraStream from adapters module"
```

---

## Task 15: `FakeQuestServer` test helper

An aiohttp-based in-process Quest server for the end-to-end integration test. Keep it in `tests/helpers/` so future adapters (or real-hardware test fixtures) can reuse it.

**Files:**
- Create: `tests/helpers/__init__.py` (if missing)
- Create: `tests/helpers/fake_quest_server.py`
- Create: `tests/helpers/test_fake_quest_server.py`

- [ ] **Step 15.1: Verify `tests/helpers/` doesn't exist; create it**

Run: `ls tests/helpers 2>/dev/null || echo missing`
If missing:
```bash
mkdir -p tests/helpers
touch tests/helpers/__init__.py
```

- [ ] **Step 15.2: Write failing test for the fake server**

Create `tests/helpers/test_fake_quest_server.py`:

```python
"""Sanity test for the in-process fake Quest HTTP server."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.fake_quest_server import FakeQuestServer


@pytest.mark.asyncio
async def test_fake_server_serves_status_and_recording_roundtrip(tmp_path):
    server = FakeQuestServer(left_mp4=b"LEFT", right_mp4=b"RIGHT")
    async with server.run() as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            r = await client.get("/status")
            assert r.status_code == 200
            assert r.json()["left_camera_ready"] is True

            r = await client.post("/recording/start", json={
                "session_id": "ep_t", "host_mono_ns": 1,
                "resolution": {"width": 1280, "height": 720}, "fps": 30,
            })
            assert r.status_code == 200

            r = await client.post("/recording/stop", json={})
            assert r.status_code == 200

            r = await client.get("/recording/files/left")
            assert r.status_code == 200
            assert r.content == b"LEFT"
```

- [ ] **Step 15.3: Run test — verify fails**

Run: `.venv/bin/python -m pytest tests/helpers/test_fake_quest_server.py -x --no-header`
Expected: `ImportError: cannot import name 'FakeQuestServer'`

- [ ] **Step 15.4: Implement `FakeQuestServer`**

Write `tests/helpers/fake_quest_server.py`:

```python
"""In-process aiohttp server that mimics the Quest 3 companion Unity app's
HTTP surface, for integration tests that don't want a real headset."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import AsyncIterator

from aiohttp import web


@dataclass
class FakeQuestServer:
    left_mp4: bytes = b""
    right_mp4: bytes = b""
    left_timestamps: bytes = b'{"frame_number":0,"capture_ns":1}\n'
    right_timestamps: bytes = b'{"frame_number":0,"capture_ns":1}\n'

    _state: dict = field(default_factory=lambda: {"recording": False, "session_id": None})

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[str]:
        app = web.Application()
        app.router.add_get("/status", self._status)
        app.router.add_post("/recording/start", self._start)
        app.router.add_post("/recording/stop", self._stop)
        app.router.add_get("/recording/files/left",  self._make_file_handler(lambda: self.left_mp4))
        app.router.add_get("/recording/files/right", self._make_file_handler(lambda: self.right_mp4))
        app.router.add_get("/recording/timestamps/left",  self._make_file_handler(lambda: self.left_timestamps))
        app.router.add_get("/recording/timestamps/right", self._make_file_handler(lambda: self.right_timestamps))

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="127.0.0.1", port=0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            await runner.cleanup()

    async def _status(self, request: web.Request) -> web.Response:
        return web.json_response({
            "recording": self._state["recording"],
            "session_id": self._state["session_id"],
            "last_preview_capture_ns": 0,
            "left_camera_ready": True,
            "right_camera_ready": True,
            "storage_free_bytes": 10_000_000_000,
        })

    async def _start(self, request: web.Request) -> web.Response:
        payload = await request.json()
        self._state["recording"] = True
        self._state["session_id"] = payload["session_id"]
        return web.json_response({
            "session_id": payload["session_id"],
            "quest_mono_ns_at_start": 0,
            "delta_ns": 0,
            "started": True,
        })

    async def _stop(self, request: web.Request) -> web.Response:
        self._state["recording"] = False
        return web.json_response({
            "session_id": self._state["session_id"],
            "left":  {"frame_count": 1, "bytes": len(self.left_mp4),  "last_capture_ns": 1},
            "right": {"frame_count": 1, "bytes": len(self.right_mp4), "last_capture_ns": 1},
            "duration_s": 0.1,
        })

    def _make_file_handler(self, getter):
        async def handler(request: web.Request) -> web.Response:
            data = getter()
            return web.Response(body=data, headers={"Content-Length": str(len(data))})
        return handler
```

- [ ] **Step 15.5: Install aiohttp + pytest-asyncio if not already present**

Run: `grep -E "aiohttp|pytest-asyncio" pyproject.toml`
If missing either, add to `[project.optional-dependencies]` `dev` list:

```toml
"aiohttp>=3.9",
"pytest-asyncio>=0.23",
```

Then: `uv sync --all-extras`

- [ ] **Step 15.6: Enable asyncio pytest plugin**

Ensure `pyproject.toml` has under `[tool.pytest.ini_options]`:

```toml
asyncio_mode = "auto"
```

If not present, add it. Do not change other pytest options.

- [ ] **Step 15.7: Run test — verify passes**

Run: `.venv/bin/python -m pytest tests/helpers/test_fake_quest_server.py -v --no-header`
Expected: `1 passed`

- [ ] **Step 15.8: Commit**

```bash
git add tests/helpers pyproject.toml uv.lock
git commit -m "test(meta_quest_camera): FakeQuestServer in-process aiohttp fixture"
```

---

## Task 16: E2E integration test with orchestrator

**Files:**
- Create: `tests/integration/adapters/test_meta_quest_camera_e2e.py`

- [ ] **Step 16.1: Write integration test**

Create `tests/integration/adapters/test_meta_quest_camera_e2e.py`:

```python
"""End-to-end: adapter + SessionOrchestrator + FakeQuestServer."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest

from syncfield.adapters import MetaQuestCameraStream
from syncfield.orchestrator import SessionOrchestrator
from tests.helpers.fake_quest_server import FakeQuestServer


@pytest.mark.asyncio
async def test_orchestrator_drives_adapter_end_to_end(tmp_path: Path):
    server = FakeQuestServer(left_mp4=b"LEFT_PAYLOAD", right_mp4=b"RIGHT_PAYLOAD")
    async with server.run() as base_url:
        parsed = urlparse(base_url)
        stream = MetaQuestCameraStream(
            id="quest_cam",
            quest_host=parsed.hostname,
            quest_port=parsed.port,
            output_dir=tmp_path,
        )
        orch = SessionOrchestrator(host_id="test-host", output_dir=tmp_path)
        orch.add(stream)
        orch.connect()
        orch.start()
        orch.stop()
        orch.disconnect()

    # Confirm artifacts landed.
    assert (tmp_path / "quest_cam_left.mp4").read_bytes() == b"LEFT_PAYLOAD"
    assert (tmp_path / "quest_cam_right.mp4").read_bytes() == b"RIGHT_PAYLOAD"
    assert (tmp_path / "quest_cam_left.timestamps.jsonl").exists()
    assert (tmp_path / "quest_cam_right.timestamps.jsonl").exists()
```

- [ ] **Step 16.2: Run integration test — verify passes**

Run: `.venv/bin/python -m pytest tests/integration/adapters/test_meta_quest_camera_e2e.py -v --no-header`
Expected: `1 passed` — if the orchestrator has different `connect()`/`start()` signatures, read `src/syncfield/orchestrator.py` and adapt the test to match existing integration-test patterns in `tests/integration/`.

If the signature differs: inspect an existing passing integration test (e.g. `tests/integration/test_round_trip.py`) and mirror its orchestrator usage.

- [ ] **Step 16.3: Commit**

```bash
git add tests/integration/adapters/test_meta_quest_camera_e2e.py
git commit -m "test(meta_quest_camera): end-to-end orchestrator + FakeQuestServer integration"
```

---

## Task 17: Manual QA checklist (docs)

Once the Unity side is implemented on real hardware, a developer runs this checklist before merging the feature. Ship it with the feature so future maintainers know how to validate the adapter.

**Files:**
- Create: `docs/meta_quest_camera_hardware_qa.md`

- [ ] **Step 17.1: Write the checklist**

Create `docs/meta_quest_camera_hardware_qa.md`:

```markdown
# Meta Quest 3 Camera Adapter — Hardware QA Checklist

Run this against a physical Quest 3 + Macbook before merging any change to
the `MetaQuestCameraStream` adapter or its Unity counterparts.

## Pre-flight

- [ ] Quest 3 running Horizon OS v74+ with the SyncField companion Unity app built and installed
- [ ] Camera + microphone permissions granted on Quest
- [ ] Quest and Macbook on the same WiFi 5/6 network
- [ ] `uv run pytest tests/` passes on Mac before hardware test

## Feasibility probe (Unity-side, one-time per Unity change)

- [ ] Unity feasibility scene sustains 30 fps on both cameras simultaneously for 3 minutes
- [ ] Hand tracking packets continue to arrive at 72 Hz during camera capture (check UDPTrackingSender logs)
- [ ] H.264 hardware encoder reports no dropped frames (MediaCodec logs)

## Adapter integration

- [ ] `MetaQuestCameraStream(...)` connects successfully after discovery finds the Quest
- [ ] Viewer shows both preview frames within 2 s of `connect()`
- [ ] Preview stays live through a 3-min idle period
- [ ] `start_recording()` returns without errors
- [ ] `SampleEvent`s arrive at ~30 Hz during recording (left-eye driven)
- [ ] `stop_recording()` completes within 30 s of stop for a 3-min session
- [ ] Four output files exist with the expected names and non-zero sizes
- [ ] MP4 files play back in VLC (visual smoke test)
- [ ] Per-eye `.timestamps.jsonl` lines all have `clock_domain == "remote_quest3"` and monotonic `capture_ns`

## Error paths

- [ ] Turning WiFi off mid-recording surfaces a health event within ~2 s
- [ ] Quest running out of storage surfaces a warning via `/status`
- [ ] Killing the Unity app mid-session causes `stop_recording()` to return `status="failed"` with a descriptive error
```

- [ ] **Step 17.2: Commit**

```bash
git add docs/meta_quest_camera_hardware_qa.md
git commit -m "docs(meta_quest_camera): hardware QA checklist for pre-merge validation"
```

---

## Final Verification

- [ ] **Full Python adapter test suite passes**

Run: `.venv/bin/python -m pytest tests/unit/adapters/meta_quest_camera tests/helpers tests/integration/adapters/test_meta_quest_camera_e2e.py -v --no-header`
Expected: all tests pass.

- [ ] **Baseline meta_quest (hand-tracking) tests still pass**

Run: `.venv/bin/python -m pytest tests/unit/adapters/test_meta_quest.py -v --no-header`
Expected: 26 passed (no regression from earlier PR).

- [ ] **Push branch and open PR when ready**

```bash
git push -u origin feat/metaquest-stereo-camera
gh pr create --title "feat(meta_quest_camera): stereo passthrough camera adapter" --body "..."
```

PR body should:
- Link to `docs/superpowers/specs/2026-04-13-metaquest-stereo-camera-design.md`
- Link to `docs/superpowers/plans/2026-04-13-metaquest-stereo-camera-python.md`
- Call out that Unity-side changes live in `opengraph-studio/unity` (separate PR)
- Include the hardware QA checklist reference

---

## Known Follow-Ups (not part of this plan)

1. Unity-side implementation (separate plan, different repo)
2. Feasibility probe (Unity throwaway scene)
3. Discovery integration — automatic host/port resolution when user omits `quest_host`
4. Viewer UI changes to show both `latest_frame_left` and `latest_frame_right` side-by-side
5. Multi-host coordination: when the Quest camera stream is a follower in a multi-host session
6. Post-pull cleanup — call `DELETE /recording/files` (spec §5.7) from `RecordingFilePuller.pull_all()` as a best-effort cleanup so long-running setups don't fill Quest storage over time. Deliberately deferred to a follow-up because it adds a failure mode to `stop_recording()` and is not required for first-use correctness (users can manually clear files between sessions).
