# OpenCV → PyAV Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace OpenCV (`cv2`) with PyAV (`av`) across syncfield-python's video capture / encoding paths so the SDK gains UVC format negotiation (e.g. 720p request), hardware-accelerated H.264 encoding (VideoToolbox on macOS), and a lighter runtime footprint — while preserving every existing Stream SPI contract (`prepare → connect → start_recording → stop_recording → disconnect`, legacy `start/stop`, `latest_frame`, `FinalizationReport`) and the host-monotonic timestamping policy (`time.monotonic_ns()` immediately after the frame is read).

**Architecture:** Introduce a shared internal `VideoEncoder` module that wraps PyAV's output container / H.264 encoder (auto-selecting `h264_videotoolbox` on macOS, falling back to `libx264`). `UVCWebcamStream` replaces `cv2.VideoCapture` with a PyAV input container (`avfoundation` / `v4l2` / `dshow`) and uses the shared encoder for MP4 output. `OakCameraStream` keeps its DepthAI capture path and only swaps `cv2.VideoWriter` for the shared encoder. The viewer's MJPEG endpoint swaps `cv2.imencode` for Pillow. Frames remain in numpy BGR so the existing `latest_frame` contract and viewer both stay untouched at the boundary. Timestamps stay `time.monotonic_ns()` — PTS is intentionally **not** adopted (see `docs/` conversation history for the rationale).

**Tech Stack:**
- `av` (PyAV ≥ 12.0) — video I/O, demux, encode
- `Pillow` (PIL ≥ 10.0) — single-frame JPEG encoding in the viewer
- `numpy` — frame buffers (already a transitive dep)
- `depthai` — unchanged for OAK capture
- `pytest` — unit tests with `sys.modules` mocking for `av` and `PIL`

---

## File Structure

**New files:**
- `src/syncfield/adapters/_video_encoder.py` — shared PyAV H.264 MP4 writer used by UVC and OAK. One responsibility: "accept BGR numpy frames at a fixed rate, produce a playable MP4."
- `tests/unit/adapters/test_video_encoder.py` — unit tests for the encoder module using a fake `av` module.

**Modified files:**
- `src/syncfield/adapters/uvc_webcam.py` — swap `cv2.VideoCapture` for PyAV input container; swap writer for `VideoEncoder`; drop the `cv2` import.
- `src/syncfield/adapters/oak_camera.py` — swap `cv2.VideoWriter` for `VideoEncoder`; drop the `cv2` import. DepthAI capture path is unchanged.
- `src/syncfield/viewer/server.py` — swap `cv2.imencode(".jpg", …)` for `PIL.Image.save(…, format="JPEG")`; drop the `cv2` import.
- `src/syncfield/types.py` — add optional `jitter_p95_ns` / `jitter_p99_ns` fields to `FinalizationReport` (both default `None` so unchanged for non-video streams).
- `tests/unit/adapters/test_uvc_webcam.py` — replace `mock_cv2` fixture with `mock_av` fixture.
- `tests/unit/adapters/test_oak_camera.py` — replace the writer-related `cv2` mocks with `mock_av` fixture. DepthAI mock unchanged.
- `tests/unit/viewer/test_cluster_endpoints.py` — update any `cv2.imencode` mock to `PIL.Image` (only if present).
- `pyproject.toml` — remove `opencv-python`, add `av` and `Pillow` to the relevant extras.

**Rationale for the split:** UVC and OAK both need an "MP4 writer that accepts numpy BGR frames and is hardware-accelerated on macOS." Duplicating that across two adapters would invite divergence. A single `VideoEncoder` with a clear interface (`open(path, width, height, fps) → encoder`; `encoder.write(frame_bgr)`; `encoder.close() → None`) isolates the PyAV detail and makes both adapters one-liners at the write site. The module lives under `adapters/` (not a top-level `video/`) because nothing outside adapter internals should depend on it.

---

## Task 0: Create the worktree and verify baseline

**Files:**
- Read: `pyproject.toml` (verify baseline test pass)

- [ ] **Step 1: Create an isolated worktree for the migration**

```bash
cd /Users/jerry/Documents/syncfield-python
git worktree add ../syncfield-python-pyav -b feat/pyav-migration
cd ../syncfield-python-pyav
```

- [ ] **Step 2: Verify the baseline test suite passes on `main`**

```bash
uv sync --all-extras
uv run pytest tests/unit -x -q
```

Expected: all tests pass. Record the pass count (it's the floor we must preserve).

- [ ] **Step 3: Commit the worktree marker (no file changes)**

Skip — the worktree exists on the branch with no changes yet.

---

## Task 1: Add `av` and `Pillow` dependencies

**Files:**
- Modify: `pyproject.toml` (extras `uvc`, `oak`, `viewer`)

- [ ] **Step 1: Inspect the current extras**

Run: `grep -n -A2 '^\[project.optional-dependencies\]' pyproject.toml`

Expected to see (roughly):
```
uvc = ["opencv-python>=4.5"]
oak = ["depthai>=3.0.0"]
viewer = ["opencv-python>=4.8.0", ...]
```

- [ ] **Step 2: Modify extras — replace `opencv-python` with `av` + `Pillow`**

Exact edits in `pyproject.toml`:

```toml
[project.optional-dependencies]
uvc = ["av>=12.0.0"]
oak = ["depthai>=3.0.0", "av>=12.0.0"]
viewer = [
    "av>=12.0.0",
    "Pillow>=10.0.0",
    # ...keep the other viewer deps (fastapi, uvicorn, etc.) exactly as they were
]
```

Also update `all = [...]` to union these (no `opencv-python` anywhere).

- [ ] **Step 3: Resolve and install**

```bash
uv sync --all-extras
```

Expected: `opencv-python` is gone from `uv.lock`; `av` and `Pillow` are added.

- [ ] **Step 4: Verify `av` imports and an encoder exists**

```bash
uv run python -c "import av; c = av.codec.Codec('h264', 'w'); print(c.name, c.long_name)"
```

Expected: `h264 ...` printed without error.

- [ ] **Step 5: Verify VideoToolbox is available on macOS (informational)**

```bash
uv run python -c "import av; print(av.codec.Codec('h264_videotoolbox', 'w').name)"
```

On Apple Silicon: prints `h264_videotoolbox`. On other platforms: raises — expected, we fall back to `libx264`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: replace opencv-python with av + Pillow in extras"
```

---

## Task 2: Introduce `VideoEncoder` — interface + failing test

**Files:**
- Create: `src/syncfield/adapters/_video_encoder.py`
- Create: `tests/unit/adapters/test_video_encoder.py`

- [ ] **Step 1: Write the failing contract test**

Create `tests/unit/adapters/test_video_encoder.py`:

```python
"""Unit tests for VideoEncoder — the shared PyAV MP4 writer.

The real ``av`` module is replaced with a fake in ``conftest.py`` style so
these tests do not depend on a working FFmpeg build. We only assert that:

* The encoder opens an output container with the right path and format.
* It adds one video stream with the requested width, height, fps, pixel
  format and a usable codec (h264_videotoolbox on mac, libx264 elsewhere).
* ``write(frame)`` encodes and muxes one packet per call.
* ``close()`` flushes the encoder and closes the container exactly once.
* Double ``close()`` is idempotent (no double-flush, no exception).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest


@pytest.fixture
def fake_av(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Install a fake ``av`` module that records every interaction."""
    container = MagicMock(name="OutputContainer")
    stream = MagicMock(name="VideoStream")
    stream.width = 0
    stream.height = 0
    stream.pix_fmt = "yuv420p"
    container.add_stream.return_value = stream

    # ``encode`` returns a list of packets; we fake one packet per call.
    packet = MagicMock(name="Packet")
    stream.encode.return_value = [packet]

    av = SimpleNamespace()
    av.open = MagicMock(name="av.open", return_value=container)

    # ``av.VideoFrame.from_ndarray`` returns a fake frame carrying the array.
    def _from_ndarray(arr: np.ndarray, format: str) -> MagicMock:
        frame = MagicMock(name="VideoFrame")
        frame.to_ndarray = lambda format="bgr24": arr
        frame._source_format = format
        return frame

    video_frame = SimpleNamespace(from_ndarray=MagicMock(side_effect=_from_ndarray))
    av.VideoFrame = video_frame

    # ``av.codec.Codec`` returns an object if the codec exists, raises if not.
    def _codec(name: str, mode: str) -> SimpleNamespace:
        if name in {"h264_videotoolbox", "libx264"}:
            return SimpleNamespace(name=name)
        raise ValueError(f"unknown codec {name}")

    av.codec = SimpleNamespace(Codec=MagicMock(side_effect=_codec))

    monkeypatch.setitem(sys.modules, "av", av)
    return SimpleNamespace(av=av, container=container, stream=stream, packet=packet)


def test_open_creates_output_container(tmp_path: Path, fake_av: SimpleNamespace) -> None:
    from syncfield.adapters._video_encoder import VideoEncoder

    out = tmp_path / "clip.mp4"
    enc = VideoEncoder.open(out, width=1280, height=720, fps=30.0)

    fake_av.av.open.assert_called_once()
    args, kwargs = fake_av.av.open.call_args
    assert args[0] == str(out)
    assert kwargs.get("mode") == "w"

    fake_av.container.add_stream.assert_called_once()
    stream_args, stream_kwargs = fake_av.container.add_stream.call_args
    # codec preference: h264_videotoolbox (macOS) or libx264 (fallback)
    assert stream_args[0] in {"h264_videotoolbox", "libx264"}
    assert stream_kwargs.get("rate") == 30
    assert fake_av.stream.width == 1280
    assert fake_av.stream.height == 720

    enc.close()


def test_write_encodes_and_muxes_one_frame(tmp_path: Path, fake_av: SimpleNamespace) -> None:
    from syncfield.adapters._video_encoder import VideoEncoder

    enc = VideoEncoder.open(tmp_path / "clip.mp4", width=64, height=48, fps=30.0)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)

    enc.write(frame)

    fake_av.av.VideoFrame.from_ndarray.assert_called_once()
    fake_av.stream.encode.assert_called()
    fake_av.container.mux.assert_called_with(fake_av.packet)

    enc.close()


def test_close_is_idempotent(tmp_path: Path, fake_av: SimpleNamespace) -> None:
    from syncfield.adapters._video_encoder import VideoEncoder

    enc = VideoEncoder.open(tmp_path / "clip.mp4", width=64, height=48, fps=30.0)
    enc.close()
    enc.close()  # should not raise, should not double-close

    assert fake_av.container.close.call_count == 1
```

- [ ] **Step 2: Run — expect failure (module missing)**

```bash
uv run pytest tests/unit/adapters/test_video_encoder.py -v
```

Expected: `ModuleNotFoundError: No module named 'syncfield.adapters._video_encoder'`.

- [ ] **Step 3: Implement `VideoEncoder`**

Create `src/syncfield/adapters/_video_encoder.py`:

```python
"""VideoEncoder — shared PyAV-based MP4 writer for video adapters.

Used by :class:`~syncfield.adapters.uvc_webcam.UVCWebcamStream` and
:class:`~syncfield.adapters.oak_camera.OakCameraStream`. The interface is
deliberately narrow: open with geometry, write BGR numpy frames, close.

The encoder auto-selects the best available H.264 encoder:
* ``h264_videotoolbox`` on macOS (hardware, near-zero CPU)
* ``libx264`` everywhere else (software, widely available)

All frames are assumed to be BGR24 (numpy ``uint8``, shape
``(height, width, 3)``) to match the rest of the SDK's convention.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

try:
    import av  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - exercised via sys.modules patch
    raise ImportError(
        "syncfield video adapters require PyAV. "
        "Install with `pip install syncfield[uvc]` (or [oak], [viewer])."
    ) from exc


def _pick_h264_encoder() -> str:
    """Return the best H.264 encoder name available in this FFmpeg build."""
    for candidate in ("h264_videotoolbox", "libx264"):
        try:
            av.codec.Codec(candidate, "w")
        except Exception:  # noqa: BLE001 - PyAV raises generic errors here
            continue
        return candidate
    raise RuntimeError(
        "No H.264 encoder found in PyAV. Reinstall `av` with libx264 support."
    )


class VideoEncoder:
    """Thin wrapper around an ``av`` output container + H.264 stream."""

    def __init__(
        self,
        container: "av.container.OutputContainer",
        stream: "av.video.stream.VideoStream",
    ) -> None:
        self._container = container
        self._stream = stream
        self._closed = False

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        width: int,
        height: int,
        fps: float,
        codec: Optional[str] = None,
        pixel_format: str = "yuv420p",
    ) -> "VideoEncoder":
        """Open ``path`` for writing and configure the H.264 stream."""
        chosen_codec = codec or _pick_h264_encoder()
        container = av.open(str(path), mode="w")
        stream = container.add_stream(chosen_codec, rate=int(round(fps)))
        stream.width = int(width)
        stream.height = int(height)
        stream.pix_fmt = pixel_format
        return cls(container, stream)

    def write(self, frame_bgr: np.ndarray) -> None:
        """Encode and mux a single BGR frame.

        Must not be called after :meth:`close`. Callers that interleave
        writes with other hot-path work should keep the frame buffer
        alive until this call returns.
        """
        if self._closed:
            raise RuntimeError("VideoEncoder.write called after close")
        video_frame = av.VideoFrame.from_ndarray(frame_bgr, format="bgr24")
        for packet in self._stream.encode(video_frame):
            self._container.mux(packet)

    def close(self) -> None:
        """Flush the encoder and close the container. Idempotent."""
        if self._closed:
            return
        self._closed = True
        # Flush: passing None drains any remaining packets in the encoder.
        try:
            for packet in self._stream.encode(None):
                self._container.mux(packet)
        finally:
            self._container.close()
```

- [ ] **Step 4: Run the test — expect pass**

```bash
uv run pytest tests/unit/adapters/test_video_encoder.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/_video_encoder.py tests/unit/adapters/test_video_encoder.py
git commit -m "feat(adapters): add VideoEncoder shared PyAV MP4 writer"
```

---

## Task 3: Build `_open_capture` platform helper — UVC input via PyAV

**Files:**
- Modify: `src/syncfield/adapters/_video_encoder.py` — add a sibling helper for input-side platform dispatch (keeping video I/O internals in one module).
- Modify: `tests/unit/adapters/test_video_encoder.py` — add tests for the helper.

Rationale: opening a UVC device with PyAV is platform-dependent (macOS uses `avfoundation`, Linux `v4l2`, Windows `dshow`). That logic is small but non-trivial; keeping it next to the encoder keeps the "video I/O primitives" in one place.

- [ ] **Step 1: Add failing tests for `open_uvc_input`**

Append to `tests/unit/adapters/test_video_encoder.py`:

```python
def test_open_uvc_input_macos(monkeypatch: pytest.MonkeyPatch, fake_av: SimpleNamespace) -> None:
    from syncfield.adapters import _video_encoder

    monkeypatch.setattr(_video_encoder.sys, "platform", "darwin")
    input_container = MagicMock(name="InputContainer")
    fake_av.av.open.return_value = input_container

    result = _video_encoder.open_uvc_input(
        device_index=0, width=1280, height=720, fps=30.0
    )

    args, kwargs = fake_av.av.open.call_args
    assert args[0] == "0:none"  # avfoundation URL: "<video>:<audio>"
    assert kwargs.get("format") == "avfoundation"
    assert kwargs.get("options", {}).get("video_size") == "1280x720"
    assert kwargs.get("options", {}).get("framerate") == "30"
    assert result is input_container


def test_open_uvc_input_linux(monkeypatch: pytest.MonkeyPatch, fake_av: SimpleNamespace) -> None:
    from syncfield.adapters import _video_encoder

    monkeypatch.setattr(_video_encoder.sys, "platform", "linux")
    fake_av.av.open.return_value = MagicMock(name="InputContainer")

    _video_encoder.open_uvc_input(
        device_index=2, width=1280, height=720, fps=30.0
    )

    args, kwargs = fake_av.av.open.call_args
    assert args[0] == "/dev/video2"
    assert kwargs.get("format") == "v4l2"


def test_open_uvc_input_windows(monkeypatch: pytest.MonkeyPatch, fake_av: SimpleNamespace) -> None:
    from syncfield.adapters import _video_encoder

    monkeypatch.setattr(_video_encoder.sys, "platform", "win32")
    fake_av.av.open.return_value = MagicMock(name="InputContainer")

    _video_encoder.open_uvc_input(
        device_index=0, width=1280, height=720, fps=30.0,
        device_name="Logitech BRIO",
    )

    args, kwargs = fake_av.av.open.call_args
    assert args[0] == "video=Logitech BRIO"
    assert kwargs.get("format") == "dshow"
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/unit/adapters/test_video_encoder.py -v -k open_uvc_input
```

Expected: `AttributeError: module 'syncfield.adapters._video_encoder' has no attribute 'open_uvc_input'`.

- [ ] **Step 3: Implement `open_uvc_input` + expose `sys`**

Edit `src/syncfield/adapters/_video_encoder.py` — add at the top (after `import numpy as np`):

```python
import sys
```

Then add, after the `VideoEncoder` class:

```python
def open_uvc_input(
    *,
    device_index: int,
    width: int,
    height: int,
    fps: float,
    device_name: Optional[str] = None,
    pixel_format: str = "mjpeg",
) -> "av.container.InputContainer":
    """Open a UVC webcam as a PyAV input container.

    Platform dispatch:

    * macOS — ``avfoundation`` with URL ``"<video>:<audio>"``. We pass
      ``"<index>:none"`` so no audio input is opened.
    * Linux — ``v4l2`` with URL ``/dev/video<N>``.
    * Windows — ``dshow`` with URL ``video=<device_name>``. The caller
      must supply ``device_name`` (DirectShow has no index URL).

    The returned container yields packets via ``.demux()`` which the
    caller decodes frame-by-frame.
    """
    options = {
        "video_size": f"{int(width)}x{int(height)}",
        "framerate": str(int(round(fps))),
        "pixel_format": pixel_format,
    }

    if sys.platform == "darwin":
        url = f"{int(device_index)}:none"
        fmt = "avfoundation"
    elif sys.platform.startswith("linux"):
        url = f"/dev/video{int(device_index)}"
        fmt = "v4l2"
    elif sys.platform.startswith("win"):
        if not device_name:
            raise ValueError(
                "Windows UVC input requires `device_name` "
                "(DirectShow has no device-index URL)."
            )
        url = f"video={device_name}"
        fmt = "dshow"
    else:
        raise RuntimeError(f"Unsupported platform for UVC input: {sys.platform}")

    return av.open(url, format=fmt, options=options)
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/unit/adapters/test_video_encoder.py -v
```

Expected: 6 passed (3 VideoEncoder + 3 platform dispatch).

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/_video_encoder.py tests/unit/adapters/test_video_encoder.py
git commit -m "feat(adapters): add open_uvc_input platform dispatch helper"
```

---

## Task 4: Migrate `UVCWebcamStream` — capture + write via PyAV

**Files:**
- Modify: `src/syncfield/adapters/uvc_webcam.py` (entire body)
- Reference: `tests/unit/adapters/test_uvc_webcam.py` (will be updated in Task 5)

Strategy: keep the public contract (constructor args, properties, lifecycle) **exactly** the same. Only the internal implementation changes.

- [ ] **Step 1: Rewrite `uvc_webcam.py`**

Replace the entire file with:

```python
"""UVCWebcamStream — PyAV-based adapter for UVC/USB webcams.

Requires the optional ``uvc`` extra:

    pip install syncfield[uvc]

The adapter runs a background thread that decodes frames from a PyAV
input container, timestamps each decoded frame with
``time.monotonic_ns()`` **before** any further processing, and publishes
them to :attr:`latest_frame` for the viewer to preview. The **same
thread** writes to an MP4 via :class:`~._video_encoder.VideoEncoder` and
emits :class:`~syncfield.types.SampleEvent` — but only while the session
is in :attr:`~syncfield.SessionState.RECORDING`.

Lifecycle and public surface are identical to the OpenCV-based
predecessor; only the internals changed.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Optional

from syncfield.adapters._video_encoder import VideoEncoder, open_uvc_input
from syncfield.clock import SessionClock
from syncfield.stream import DeviceKey, StreamBase
from syncfield.types import (
    FinalizationReport,
    SampleEvent,
    StreamCapabilities,
)


class UVCWebcamStream(StreamBase):
    """Captures video from a UVC webcam via PyAV.

    Args:
        id: Stream id (also the output file name, ``{id}.mp4``).
        device_index: Platform device index (AVFoundation / V4L2 index,
            or DirectShow fallback alongside ``device_name``).
        output_dir: Directory for the resulting MP4 file.
        width: Requested frame width. Defaults to 1280 (720p).
        height: Requested frame height. Defaults to 720 (720p).
        fps: Requested frame rate. Defaults to 30.0.
        device_name: Required only on Windows (DirectShow). Ignored
            on macOS / Linux.
    """

    _discovery_kind = "video"
    _discovery_adapter_type = "uvc_webcam"

    def __init__(
        self,
        id: str,
        device_index: int,
        output_dir: Path | str,
        width: Optional[int] = 1280,
        height: Optional[int] = 720,
        fps: Optional[float] = 30.0,
        device_name: Optional[str] = None,
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
        self._device_index = device_index
        self._device_name = device_name
        self._output_dir = Path(output_dir)
        self._width = int(width) if width else 1280
        self._height = int(height) if height else 720
        self._fps = float(fps) if fps else 30.0

        self._input: Any = None
        self._encoder: Optional[VideoEncoder] = None
        self._file_path = self._output_dir / f"{id}.mp4"

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._frame_count = 0
        self._first_at: Optional[int] = None
        self._last_at: Optional[int] = None

        self._recording = False

        self._frame_lock = threading.Lock()
        self._latest_frame: Any = None

    @property
    def device_key(self) -> Optional[DeviceKey]:
        return ("uvc_webcam", str(self._device_index))

    # ------------------------------------------------------------------
    # Stream SPI — 4-phase lifecycle
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        """Open the PyAV input container."""
        if self._input is None:
            self._input = open_uvc_input(
                device_index=self._device_index,
                width=self._width,
                height=self._height,
                fps=self._fps,
                device_name=self._device_name,
            )

    def connect(self) -> None:
        """Start the capture thread in preview-only mode."""
        if self._thread is not None and self._thread.is_alive():
            return
        if self._input is None:
            self.prepare()
        self._recording = False
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, name=f"uvc-{self.id}", daemon=True
        )
        self._thread.start()

    def start_recording(self, session_clock: SessionClock) -> None:
        """Open the VideoEncoder and flip recording on."""
        if self._thread is None or not self._thread.is_alive():
            self.connect()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._encoder = VideoEncoder.open(
            self._file_path,
            width=self._width,
            height=self._height,
            fps=self._fps,
        )
        self._recording = True

    def stop_recording(self) -> FinalizationReport:
        """Flip recording off, close the encoder, emit the report."""
        self._recording = False
        if self._encoder is not None:
            self._encoder.close()
            self._encoder = None
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=self._frame_count,
            file_path=self._file_path if self._frame_count > 0 else None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=None,
        )

    def disconnect(self) -> None:
        """Stop the capture thread and release the input container."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._release_av_resources()

    # ------------------------------------------------------------------
    # Legacy one-shot lifecycle
    # ------------------------------------------------------------------

    def start(self, session_clock: SessionClock) -> None:
        if self._input is None:
            self.prepare()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._encoder = VideoEncoder.open(
            self._file_path,
            width=self._width,
            height=self._height,
            fps=self._fps,
        )
        self._recording = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, name=f"uvc-{self.id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> FinalizationReport:
        report = self.stop_recording()
        self.disconnect()
        return report

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        """Background thread — decode frames in a tight loop.

        Two phases, distinguished by the ``_recording`` flag:

        * Preview — publish to ``latest_frame`` only.
        * Recording — also encode to MP4 and emit SampleEvent.

        The loop exits when ``_stop_event`` fires or the input
        container is exhausted (device disconnect).
        """
        assert self._input is not None
        try:
            for frame in self._input.decode(video=0):
                if self._stop_event.is_set():
                    break

                capture_ns = time.monotonic_ns()
                frame_bgr = frame.to_ndarray(format="bgr24")

                with self._frame_lock:
                    self._latest_frame = frame_bgr

                if self._recording:
                    if self._first_at is None:
                        self._first_at = capture_ns
                    self._last_at = capture_ns
                    self._frame_count += 1
                    if self._encoder is not None:
                        self._encoder.write(frame_bgr)
                    self._emit_sample(
                        SampleEvent(
                            stream_id=self.id,
                            frame_number=self._frame_count - 1,
                            capture_ns=capture_ns,
                        )
                    )
        except Exception:
            # Device disconnect or decode error — exit the loop; the
            # orchestrator observes the thread dying via disconnect().
            return

    # ------------------------------------------------------------------
    # Live preview
    # ------------------------------------------------------------------

    @property
    def latest_frame(self) -> Any:
        """Return the most recently captured BGR frame, or ``None``."""
        with self._frame_lock:
            return self._latest_frame

    def _release_av_resources(self) -> None:
        if self._encoder is not None:
            self._encoder.close()
            self._encoder = None
        if self._input is not None:
            try:
                self._input.close()
            finally:
                self._input = None

    # ------------------------------------------------------------------
    # Discovery — unchanged from the OpenCV version.
    # ------------------------------------------------------------------

    @classmethod
    def discover(cls, *, timeout: float = 5.0) -> list:
        # Keep the existing discovery body verbatim — it does not touch
        # cv2 or av and only enumerates the system via ``system_profiler``
        # / ``/dev/video*``. Paste the original implementation here.
        raise NotImplementedError(
            "Copy the discover() body from the pre-migration file verbatim"
        )
```

> **Implementation note for the engineer:** The `discover` method's body does not depend on cv2 — copy lines 368–510 from the pre-migration `uvc_webcam.py` unchanged into the `discover` method here. The placeholder `raise` above is a reminder, not a design choice.

- [ ] **Step 2: Copy the original `discover()` body verbatim**

```bash
git show main:src/syncfield/adapters/uvc_webcam.py | sed -n '368,510p'
```

Copy that body into the `discover` method, replacing the `raise NotImplementedError(...)` line.

- [ ] **Step 3: Smoke check — import the module (tests will fail, that's Task 5)**

```bash
uv run python -c "from syncfield.adapters.uvc_webcam import UVCWebcamStream; print(UVCWebcamStream)"
```

Expected: prints the class. If it raises, fix import errors before moving on.

- [ ] **Step 4: Commit**

```bash
git add src/syncfield/adapters/uvc_webcam.py
git commit -m "feat(adapters): migrate UVCWebcamStream to PyAV"
```

---

## Task 5: Migrate UVC adapter tests

**Files:**
- Modify: `tests/unit/adapters/test_uvc_webcam.py` (replace `mock_cv2` with `mock_av`)

- [ ] **Step 1: Inspect the existing test fixture**

```bash
sed -n '1,60p' tests/unit/adapters/test_uvc_webcam.py
```

Note the `mock_cv2` fixture shape — we'll replace it with `mock_av` while keeping every test's intent.

- [ ] **Step 2: Replace the `mock_cv2` fixture with `mock_av`**

Edit `tests/unit/adapters/test_uvc_webcam.py`:

Replace the `mock_cv2` fixture with this fixture (keep any other fixtures unchanged):

```python
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest


@pytest.fixture
def mock_av(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Fake the ``av`` module so UVC tests run without FFmpeg."""

    # ---- Input side (capture) -------------------------------------
    def _make_frame(i: int) -> MagicMock:
        frame = MagicMock(name=f"Frame-{i}")
        frame.to_ndarray = MagicMock(
            return_value=np.full((48, 64, 3), i % 256, dtype=np.uint8)
        )
        return frame

    input_container = MagicMock(name="InputContainer")
    # Yield 3 frames then stop — tests that need more can override.
    input_container.decode = MagicMock(
        return_value=iter([_make_frame(i) for i in range(3)])
    )

    # ---- Output side (encoder) ------------------------------------
    output_stream = MagicMock(name="VideoStream")
    output_stream.encode = MagicMock(return_value=[MagicMock(name="Packet")])
    output_container = MagicMock(name="OutputContainer")
    output_container.add_stream = MagicMock(return_value=output_stream)

    # ---- av.open dispatches on mode -------------------------------
    def _av_open(url, *args, **kwargs):  # noqa: ANN001 - MagicMock signature
        if kwargs.get("mode") == "w":
            return output_container
        return input_container

    av = SimpleNamespace()
    av.open = MagicMock(side_effect=_av_open)
    av.VideoFrame = SimpleNamespace(
        from_ndarray=MagicMock(return_value=MagicMock(name="OutFrame"))
    )
    av.codec = SimpleNamespace(
        Codec=MagicMock(side_effect=lambda n, m: SimpleNamespace(name=n))
    )

    monkeypatch.setitem(sys.modules, "av", av)
    return SimpleNamespace(
        av=av,
        input_container=input_container,
        output_container=output_container,
        output_stream=output_stream,
        make_frame=_make_frame,
    )
```

- [ ] **Step 3: Update every test that referenced `mock_cv2`**

Find and replace at call sites:

```bash
grep -n 'mock_cv2' tests/unit/adapters/test_uvc_webcam.py
```

For each occurrence, rename the fixture parameter to `mock_av` and:

- `mock_cv2.VideoCapture.return_value.read.return_value` → remove (use `mock_av.input_container.decode`)
- `mock_cv2.VideoWriter` → remove (the writer is tested separately in `test_video_encoder.py`)
- Assertions on `write()` calls should assert on `mock_av.output_stream.encode` instead
- Assertions on frame counts should still use `FinalizationReport.frame_count` — that's unchanged

Rewrite each test to verify:
- `prepare()` calls `av.open(...)` once with the platform format
- `connect()` spawns the capture thread and begins populating `latest_frame`
- `start_recording()` opens the writer container (mode="w")
- After N decoded frames + `stop_recording()`, the `FinalizationReport` has `frame_count == N` with the expected file path

Example replacement for one test (the lifecycle happy path):

```python
def test_4phase_lifecycle_records_expected_frame_count(
    mock_av: SimpleNamespace, tmp_path
) -> None:
    from syncfield.adapters.uvc_webcam import UVCWebcamStream
    from syncfield.clock import SessionClock

    # Drive 5 frames through the decode iterator
    frames = [mock_av.make_frame(i) for i in range(5)]
    mock_av.input_container.decode = MagicMock(return_value=iter(frames))

    stream = UVCWebcamStream(
        id="uvc0", device_index=0, output_dir=tmp_path, fps=30.0
    )
    stream.prepare()
    stream.connect()
    stream.start_recording(SessionClock())

    # Let the capture thread drain the iterator.
    import time as _t
    _t.sleep(0.05)

    report = stream.stop_recording()
    stream.disconnect()

    assert report.frame_count == 5
    assert report.file_path == tmp_path / "uvc0.mp4"
    assert mock_av.output_stream.encode.call_count >= 5
```

Repeat this pattern for the other lifecycle tests (legacy `start/stop`, preview without recording, stop_recording idempotency, etc.). Each replacement must preserve the original assertion's intent.

- [ ] **Step 4: Run the UVC tests — expect all pass**

```bash
uv run pytest tests/unit/adapters/test_uvc_webcam.py -v
```

Expected: same number of passing tests as before the migration.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/adapters/test_uvc_webcam.py
git commit -m "test(adapters): migrate UVC tests from mock_cv2 to mock_av"
```

---

## Task 6: Migrate `OakCameraStream` — writer only

**Files:**
- Modify: `src/syncfield/adapters/oak_camera.py`

The OAK adapter keeps DepthAI for capture. Only the `cv2.VideoWriter` usage is swapped for `VideoEncoder`.

- [ ] **Step 1: Remove the cv2 import and import `VideoEncoder`**

Edit `src/syncfield/adapters/oak_camera.py`:

Delete the `cv2` try/except block (lines ~64–69 in the current file). Add near the other syncfield imports:

```python
from syncfield.adapters._video_encoder import VideoEncoder
```

Change the type hint for `_video_writer`:

```python
self._video_writer: Optional[VideoEncoder] = None
```

- [ ] **Step 2: Replace the `cv2.VideoWriter` construction in `start_recording`**

Find the block around line 309–313 (`fourcc = cv2.VideoWriter_fourcc...`) and replace with:

```python
width, height = self._rgb_resolution
self._video_writer = VideoEncoder.open(
    self._mp4_path,
    width=width,
    height=height,
    fps=float(self._rgb_fps),
)
```

Apply the same replacement in the legacy `start()` method if it duplicates this construction.

- [ ] **Step 3: Update the frame-write and release sites**

Frame write site (inside `_drain_rgb_tick` or wherever `self._video_writer.write(frame)` currently lives): the call signature is the same (`self._video_writer.write(frame_bgr)`), so **no change is needed** at the write site — `VideoEncoder.write()` matches the OpenCV contract.

Writer release site (`_release_writers`): replace `self._video_writer.release()` with `self._video_writer.close()`.

```bash
grep -n 'self._video_writer' src/syncfield/adapters/oak_camera.py
```

Verify every hit either stays the same (`.write(...)`) or is updated (`.release()` → `.close()`).

- [ ] **Step 4: Smoke check imports**

```bash
uv run python -c "from syncfield.adapters.oak_camera import OakCameraStream; print(OakCameraStream)"
```

Expected: prints the class.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/oak_camera.py
git commit -m "refactor(adapters): swap cv2.VideoWriter for VideoEncoder in OAK"
```

---

## Task 7: Migrate OAK adapter tests

**Files:**
- Modify: `tests/unit/adapters/test_oak_camera.py`

- [ ] **Step 1: Merge `mock_av` into the OAK test file**

Paste the `mock_av` fixture from `test_uvc_webcam.py` into `test_oak_camera.py` (or hoist both to `tests/unit/adapters/conftest.py` — prefer the conftest hoist for DRY).

- [ ] **Step 2: Hoist both fixtures to `conftest.py`**

Create or extend `tests/unit/adapters/conftest.py`:

```python
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest


@pytest.fixture
def mock_av(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Shared ``av`` module mock for adapter tests."""
    # (paste the body from Task 5 Step 2 here, verbatim)
    ...
```

Remove the duplicated fixture from `test_uvc_webcam.py`.

- [ ] **Step 3: Update the OAK `mock_cv2` fixture usage**

In `test_oak_camera.py`, find every test that used `mock_cv2` for the `VideoWriter`. Replace the fixture parameter with `mock_av` and update assertions:

- `mock_cv2.VideoWriter_fourcc.assert_called_with(*"mp4v")` → remove (fourcc is handled inside VideoEncoder, not the adapter's concern)
- `mock_cv2.VideoWriter.assert_called_once_with(...)` → `mock_av.av.open.assert_called_with(..., mode="w")` (plus the geometry on the returned stream)

The DepthAI mock (`mock_depthai`) is untouched.

- [ ] **Step 4: Run the OAK tests**

```bash
uv run pytest tests/unit/adapters/test_oak_camera.py -v
```

Expected: same number of passing tests as pre-migration.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/adapters/conftest.py tests/unit/adapters/test_oak_camera.py tests/unit/adapters/test_uvc_webcam.py
git commit -m "test(adapters): hoist mock_av fixture and migrate OAK tests"
```

---

## Task 8: Migrate viewer MJPEG endpoint — Pillow

**Files:**
- Modify: `src/syncfield/viewer/server.py`
- Modify: `tests/unit/viewer/test_cluster_endpoints.py` (if it mocks cv2)

- [ ] **Step 1: Remove `cv2` import, add Pillow**

In `src/syncfield/viewer/server.py`:

```python
# DELETE
import cv2

# ADD (near the other stdlib / third-party imports)
import io
from PIL import Image
```

- [ ] **Step 2: Replace the JPEG encode block**

Find the `_mjpeg_generator` method (around lines 1645–1666). Replace the `cv2.imencode` block with:

```python
# BGR numpy → PIL Image (PIL expects RGB, so reverse the last axis)
rgb = stream.latest_frame[:, :, ::-1]
img = Image.fromarray(rgb)
buf = io.BytesIO()
img.save(buf, format="JPEG", quality=80)
frame_bytes = buf.getvalue()
```

The `yield` block after this stays identical.

- [ ] **Step 3: Update viewer tests if they mock `cv2.imencode`**

```bash
grep -n 'cv2\|imencode' tests/unit/viewer/test_cluster_endpoints.py
```

If any test patches `cv2.imencode`, replace with a patch on `PIL.Image.Image.save` or a fake frame that round-trips through real Pillow (Pillow has no native dep, so it's fine to use the real library in tests).

- [ ] **Step 4: Run viewer tests**

```bash
uv run pytest tests/unit/viewer -v
```

Expected: same pass count as before.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/viewer/server.py tests/unit/viewer/test_cluster_endpoints.py
git commit -m "refactor(viewer): swap cv2.imencode for Pillow JPEG encoder"
```

---

## Task 9: Verify OpenCV is fully gone

**Files:**
- No edits — verification only.

- [ ] **Step 1: Grep the repo for any remaining `cv2`**

Run: `grep -rn 'cv2\|opencv' src/ tests/ --include='*.py'`

Expected: zero hits. If hits remain, fix them before continuing.

- [ ] **Step 2: Grep `pyproject.toml`**

Run: `grep -n 'opencv' pyproject.toml`

Expected: zero hits.

- [ ] **Step 3: Run the full unit test suite**

```bash
uv run pytest tests/unit -x -q
```

Expected: the pass count recorded in Task 0 Step 2.

- [ ] **Step 4: Commit the cleanup marker (docs only if anything updated)**

No changes expected — skip.

---

## Task 10: Add jitter metrics to `FinalizationReport`

**Files:**
- Modify: `src/syncfield/types.py`
- Modify: `src/syncfield/adapters/uvc_webcam.py` (collect jitter in capture loop)
- Modify: `src/syncfield/adapters/oak_camera.py` (collect jitter in capture loop)
- Create: `tests/unit/test_finalization_jitter.py`

Rationale: as decided in design discussion, we keep `time.monotonic_ns()` as the single timestamp source. To monitor whether that choice starts to hurt us (scale, CPU pressure), every video stream reports `jitter_p95_ns` and `jitter_p99_ns` in its FinalizationReport.

- [ ] **Step 1: Add the failing test**

Create `tests/unit/test_finalization_jitter.py`:

```python
from syncfield.types import FinalizationReport


def test_finalization_report_accepts_jitter_fields() -> None:
    report = FinalizationReport(
        stream_id="uvc0",
        status="completed",
        frame_count=100,
        file_path=None,
        first_sample_at_ns=0,
        last_sample_at_ns=3_000_000_000,
        health_events=[],
        error=None,
        jitter_p95_ns=2_500_000,
        jitter_p99_ns=4_000_000,
    )
    assert report.jitter_p95_ns == 2_500_000
    assert report.jitter_p99_ns == 4_000_000


def test_finalization_report_jitter_defaults_none() -> None:
    report = FinalizationReport(
        stream_id="ble0",
        status="completed",
        frame_count=0,
        file_path=None,
        first_sample_at_ns=None,
        last_sample_at_ns=None,
        health_events=[],
        error=None,
    )
    assert report.jitter_p95_ns is None
    assert report.jitter_p99_ns is None
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/unit/test_finalization_jitter.py -v
```

Expected: `TypeError: unexpected keyword argument 'jitter_p95_ns'`.

- [ ] **Step 3: Extend `FinalizationReport`**

Edit `src/syncfield/types.py`:

```python
@dataclass
class FinalizationReport:
    stream_id: str
    status: Literal["completed", "partial", "failed"]
    frame_count: int
    file_path: Path | None
    first_sample_at_ns: int | None
    last_sample_at_ns: int | None
    health_events: list[HealthEvent]
    error: str | None
    jitter_p95_ns: int | None = None
    jitter_p99_ns: int | None = None
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/unit/test_finalization_jitter.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Plumb jitter collection into the UVC capture loop**

In `src/syncfield/adapters/uvc_webcam.py`, add to `__init__`:

```python
self._prev_capture_ns: Optional[int] = None
self._intervals_ns: list[int] = []
```

In `_capture_loop`, immediately after `capture_ns = time.monotonic_ns()`:

```python
if self._prev_capture_ns is not None:
    self._intervals_ns.append(capture_ns - self._prev_capture_ns)
self._prev_capture_ns = capture_ns
```

In `stop_recording`, compute percentiles before building the report:

```python
import statistics  # at the top of the file

jitter_p95 = jitter_p99 = None
if len(self._intervals_ns) >= 20:
    sorted_iv = sorted(self._intervals_ns)
    jitter_p95 = sorted_iv[int(len(sorted_iv) * 0.95)]
    jitter_p99 = sorted_iv[int(len(sorted_iv) * 0.99)]

return FinalizationReport(
    stream_id=self.id,
    status="completed",
    frame_count=self._frame_count,
    file_path=self._file_path if self._frame_count > 0 else None,
    first_sample_at_ns=self._first_at,
    last_sample_at_ns=self._last_at,
    health_events=list(self._collected_health),
    error=None,
    jitter_p95_ns=jitter_p95,
    jitter_p99_ns=jitter_p99,
)
```

Also reset `self._intervals_ns = []` and `self._prev_capture_ns = None` at the start of `start_recording` so a second recording in the same session starts clean.

- [ ] **Step 6: Apply the identical plumbing to OAK**

Same four edits in `src/syncfield/adapters/oak_camera.py`:

1. Init: `self._prev_capture_ns`, `self._intervals_ns`.
2. Capture tick: append interval.
3. `start_recording`: reset both.
4. `stop_recording`: compute p95/p99, include in report.

- [ ] **Step 7: Update UVC lifecycle test to assert jitter**

In `tests/unit/adapters/test_uvc_webcam.py`, add one test:

```python
def test_jitter_reported_when_enough_frames(
    mock_av, tmp_path
) -> None:
    from syncfield.adapters.uvc_webcam import UVCWebcamStream
    from syncfield.clock import SessionClock
    import time as _t

    frames = [mock_av.make_frame(i) for i in range(30)]
    mock_av.input_container.decode = MagicMock(return_value=iter(frames))

    stream = UVCWebcamStream(
        id="uvc0", device_index=0, output_dir=tmp_path, fps=30.0
    )
    stream.prepare()
    stream.connect()
    stream.start_recording(SessionClock())
    _t.sleep(0.1)
    report = stream.stop_recording()
    stream.disconnect()

    assert report.jitter_p95_ns is not None
    assert report.jitter_p99_ns is not None
    assert report.jitter_p99_ns >= report.jitter_p95_ns
```

- [ ] **Step 8: Run all tests**

```bash
uv run pytest tests/unit -x -q
```

Expected: all previously-passing tests pass + the 3 new jitter tests.

- [ ] **Step 9: Commit**

```bash
git add src/syncfield/types.py src/syncfield/adapters/uvc_webcam.py \
        src/syncfield/adapters/oak_camera.py tests/
git commit -m "feat(metrics): report jitter p95/p99 in FinalizationReport"
```

---

## Task 11: Hardware smoke test (documentation, not automated)

**Files:**
- Create: `docs/plans/2026-04-13-opencv-to-pyav-migration-hw-smoketest.md`

- [ ] **Step 1: Write a smoke-test checklist for the hardware engineer**

Create the file with this body:

```markdown
# PyAV Migration — Hardware Smoke Test

Run on: MacBook M3/M4, Apple Silicon, macOS 14+.

1. **UVC camera at 720p** — plug a FaceTime / Logitech / BRIO webcam:
   ```bash
   uv run python examples/iphone_mac_webcam/run.py --width 1280 --height 720 --fps 30
   ```
   Record for 30 s. Check: `{id}.mp4` plays in QuickTime, is ~720p, has 900 ± 5 frames.

2. **VideoToolbox hardware encoder** — while recording:
   ```bash
   top -pid $(pgrep -f run.py)
   ```
   Python CPU should be < 30 % (was > 80 % on cv2 mp4v).

3. **OAK camera with depth** — run `examples/mac_iphone_dual_oak/`:
   ```bash
   uv run python examples/mac_iphone_dual_oak/run.py --duration 30
   ```
   Check: RGB mp4 is valid; `.depth.bin` size ≈ `1280*800*2*frame_count` bytes.

4. **Viewer MJPEG** — load the viewer in a browser, confirm thumbnails
   update at ~30 fps per stream for the recording window.

5. **Jitter report** — at session end, inspect
   `session_manifest.json` or the stdout report for
   `jitter_p95_ns` / `jitter_p99_ns`. On an idle M3 with 4 streams
   expect p95 < 3 ms, p99 < 6 ms.
```

- [ ] **Step 2: Commit**

```bash
git add docs/plans/2026-04-13-opencv-to-pyav-migration-hw-smoketest.md
git commit -m "docs: hardware smoke-test checklist for PyAV migration"
```

---

## Self-Review Summary

**Spec coverage:**
- UVC adapter migration → Tasks 3, 4, 5.
- OAK adapter migration (writer only) → Tasks 6, 7.
- Viewer MJPEG migration → Task 8.
- Remove `opencv-python` entirely → Tasks 1 and 9.
- Preserve existing lifecycle and contract → every task's "keep public surface identical" note.
- Jitter metric (decided in design chat) → Task 10.
- Hardware validation → Task 11.

**Placeholder scan:** one intentional placeholder in Task 4 Step 1 (the `discover` method body is a verbatim copy from the pre-migration file — Task 4 Step 2 tells the engineer exactly how to retrieve it). No generic "add error handling" or "TBD" elsewhere.

**Type consistency:** `VideoEncoder.open(...)` / `VideoEncoder.write(...)` / `VideoEncoder.close()` and `open_uvc_input(...)` are used identically across Tasks 2, 3, 4, 6. `FinalizationReport.jitter_p95_ns` / `jitter_p99_ns` field names match across Task 10 definition, plumbing, and test sites.

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-04-13-opencv-to-pyav-migration.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach do you want?
