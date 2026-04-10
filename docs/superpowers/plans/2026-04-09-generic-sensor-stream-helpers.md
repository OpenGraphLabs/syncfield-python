# Generic Sensor Stream Helpers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `PollingSensorStream` and `PushSensorStream` as first-class helpers so users can attach simple sensors in roughly one statement, without writing a full `StreamBase` subclass.

**Architecture:** Two `StreamBase` subclasses sharing a small private `_generic.py` module that owns sensor JSONL persistence (`_SensorWriteCore`). Polling helper owns a capture thread and calls a user `read()` function on a fixed `hz`. Push helper exposes a thread-safe `push(channels, capture_ns=None)` method that the user calls from their own thread/callback. Both implement the full 4-phase lifecycle (`prepare → connect → start_recording → stop_recording → disconnect`) so they get viewer parity, live preview, and re-record support for free.

**Tech Stack:** Python 3.10+, stdlib only (`threading`, `time`, `inspect`), pytest with `tmp_path` fixture, existing `syncfield.writer.SensorWriter`, existing `StreamBase` SPI.

**Spec:** [`docs/superpowers/specs/2026-04-09-generic-sensor-stream-helpers-design.md`](../specs/2026-04-09-generic-sensor-stream-helpers-design.md)

---

## File Structure

**Create:**

| Path | Responsibility |
|---|---|
| `src/syncfield/adapters/_generic.py` | Private internals: `_SensorWriteCore` (writer + frame counter + lock + first/last_at tracking), `_default_sensor_capabilities`, `_resolve_capabilities` |
| `src/syncfield/adapters/polling_sensor.py` | `PollingSensorStream` — owns one capture thread between `connect()` and `disconnect()`. Calls user `read()` on a fixed `hz`. |
| `src/syncfield/adapters/push_sensor.py` | `PushSensorStream` — exposes thread-safe `push()` for user-owned producer threads. |
| `tests/unit/adapters/test_generic_internals.py` | Unit tests for `_SensorWriteCore` and `_resolve_capabilities` |
| `tests/unit/adapters/test_polling_sensor.py` | Unit tests for `PollingSensorStream` (drives `_capture_once()` without spawning a thread) |
| `tests/unit/adapters/test_push_sensor.py` | Unit tests for `PushSensorStream` |
| `tests/integration/adapters/__init__.py` | (empty package marker, if missing) |
| `tests/integration/adapters/test_polling_sensor_threading.py` | Real-thread integration test for polling helper |
| `tests/integration/adapters/test_push_sensor_threading.py` | Multi-producer stress test for push helper |
| `tests/integration/test_generic_sensor_e2e.py` | Full session walk through `SessionOrchestrator` with both helpers |
| `examples/generic_sensor_demo/README.md` | Short README explaining the two examples |
| `examples/generic_sensor_demo/polling_serial.py` | 5-line polling example using a fake serial source |
| `examples/generic_sensor_demo/push_async.py` | 5-line asyncio example using `PushSensorStream` |

**Modify:**

| Path | Change |
|---|---|
| `src/syncfield/adapters/__init__.py` | Import and re-export `PollingSensorStream` and `PushSensorStream`; add to `__all__` |

---

## Phase 1 — Foundation (`_generic.py`)

### Task 1: `_SensorWriteCore` skeleton with open/close + frame counter

**Files:**
- Create: `src/syncfield/adapters/_generic.py`
- Test: `tests/unit/adapters/test_generic_internals.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/adapters/test_generic_internals.py
"""Tests for syncfield.adapters._generic internals."""

from __future__ import annotations

from pathlib import Path

from syncfield.adapters._generic import _SensorWriteCore


def test_sensor_write_core_frame_counter_starts_at_zero(tmp_path: Path):
    core = _SensorWriteCore("imu", tmp_path)
    assert core.next_frame_number() == 0
    assert core.next_frame_number() == 1
    assert core.next_frame_number() == 2


def test_sensor_write_core_open_creates_jsonl_file(tmp_path: Path):
    core = _SensorWriteCore("imu", tmp_path)
    core.open()
    assert (tmp_path / "imu.jsonl").exists()
    core.close()


def test_sensor_write_core_close_is_idempotent(tmp_path: Path):
    core = _SensorWriteCore("imu", tmp_path)
    core.open()
    core.close()
    core.close()  # must not raise


def test_sensor_write_core_path_property(tmp_path: Path):
    core = _SensorWriteCore("imu", tmp_path)
    assert core.path == tmp_path / "imu.jsonl"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/adapters/test_generic_internals.py -v`
Expected: ImportError — `_generic` module does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
# src/syncfield/adapters/_generic.py
"""Private internals shared by PollingSensorStream and PushSensorStream.

Nothing here is exported. The helpers compose `_SensorWriteCore` rather
than inheriting from it so the persistence and threading concerns stay
cleanly separated.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from syncfield.types import SensorSample, StreamCapabilities
from syncfield.writer import SensorWriter


class _SensorWriteCore:
    """Owns the SensorWriter, frame counter, and timing trackers for a
    single sensor stream. Thread-safe: ``write()`` may be called from
    multiple producer threads simultaneously.
    """

    def __init__(self, stream_id: str, output_dir: Path) -> None:
        self._stream_id = stream_id
        self._output_dir = Path(output_dir)
        self._writer: Optional[SensorWriter] = None
        self._lock = threading.Lock()
        self._frame_counter = 0
        self._first_at_ns: Optional[int] = None
        self._last_at_ns: Optional[int] = None

    @property
    def path(self) -> Path:
        return self._output_dir / f"{self._stream_id}.jsonl"

    @property
    def first_sample_at_ns(self) -> Optional[int]:
        return self._first_at_ns

    @property
    def last_sample_at_ns(self) -> Optional[int]:
        return self._last_at_ns

    @property
    def frame_count(self) -> int:
        return self._writer.count if self._writer is not None else 0

    def next_frame_number(self) -> int:
        with self._lock:
            n = self._frame_counter
            self._frame_counter += 1
            return n

    def open(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._writer = SensorWriter(self._stream_id, self._output_dir)
        self._writer.open()

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/adapters/test_generic_internals.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/_generic.py tests/unit/adapters/test_generic_internals.py
git commit -m "feat(adapters): _SensorWriteCore skeleton (open/close + frame counter)"
```

---

### Task 2: Thread-safe `_SensorWriteCore.write()` with first/last_at tracking

**Files:**
- Modify: `src/syncfield/adapters/_generic.py`
- Test: `tests/unit/adapters/test_generic_internals.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/unit/adapters/test_generic_internals.py`:

```python
import json
import threading

from syncfield.types import SensorSample


def test_sensor_write_core_write_appends_jsonl(tmp_path):
    core = _SensorWriteCore("imu", tmp_path)
    core.open()
    core.write(SensorSample(frame_number=0, capture_ns=1000,
                            channels={"ax": 0.1}))
    core.write(SensorSample(frame_number=1, capture_ns=2000,
                            channels={"ax": 0.2}))
    core.close()

    lines = (tmp_path / "imu.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["frame_number"] == 0
    assert first["capture_ns"] == 1000
    assert first["channels"] == {"ax": 0.1}


def test_sensor_write_core_tracks_first_and_last_at(tmp_path):
    core = _SensorWriteCore("imu", tmp_path)
    core.open()
    core.write(SensorSample(frame_number=0, capture_ns=1000, channels={"x": 1}))
    core.write(SensorSample(frame_number=1, capture_ns=2500, channels={"x": 2}))
    core.write(SensorSample(frame_number=2, capture_ns=3700, channels={"x": 3}))
    assert core.first_sample_at_ns == 1000
    assert core.last_sample_at_ns == 3700
    assert core.frame_count == 3
    core.close()


def test_sensor_write_core_write_is_thread_safe(tmp_path):
    """100 producer threads × 50 writes each = 5000 lines, all intact."""
    core = _SensorWriteCore("imu", tmp_path)
    core.open()

    def producer(tid: int) -> None:
        for i in range(50):
            core.write(SensorSample(
                frame_number=core.next_frame_number(),
                capture_ns=1000 + tid * 1000 + i,
                channels={"tid": tid, "i": i},
            ))

    threads = [threading.Thread(target=producer, args=(t,)) for t in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    core.close()

    lines = (tmp_path / "imu.jsonl").read_text().strip().split("\n")
    assert len(lines) == 5000
    # Every line must be valid JSON (no torn writes)
    for line in lines:
        json.loads(line)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/adapters/test_generic_internals.py -v`
Expected: 3 new tests fail with `AttributeError: '_SensorWriteCore' object has no attribute 'write'`.

- [ ] **Step 3: Implement `write()`**

Add to `src/syncfield/adapters/_generic.py` inside `_SensorWriteCore`:

```python
    def write(self, sample: SensorSample) -> None:
        with self._lock:
            if self._writer is None:
                raise RuntimeError(
                    f"_SensorWriteCore for '{self._stream_id}' is not open"
                )
            self._writer.write(sample)
            if self._first_at_ns is None:
                self._first_at_ns = sample.capture_ns
            self._last_at_ns = sample.capture_ns
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/adapters/test_generic_internals.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/_generic.py tests/unit/adapters/test_generic_internals.py
git commit -m "feat(adapters): _SensorWriteCore.write() with thread-safety + first/last_at tracking"
```

---

### Task 3: `_default_sensor_capabilities` + `_resolve_capabilities` helpers

**Files:**
- Modify: `src/syncfield/adapters/_generic.py`
- Test: `tests/unit/adapters/test_generic_internals.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/unit/adapters/test_generic_internals.py`:

```python
from syncfield.adapters._generic import (
    _default_sensor_capabilities,
    _resolve_capabilities,
)
from syncfield.types import StreamCapabilities


def test_default_sensor_capabilities_precise_true():
    caps = _default_sensor_capabilities(precise=True)
    assert caps.provides_audio_track is False
    assert caps.supports_precise_timestamps is True
    assert caps.is_removable is False
    assert caps.produces_file is True


def test_default_sensor_capabilities_precise_false():
    caps = _default_sensor_capabilities(precise=False)
    assert caps.supports_precise_timestamps is False


def test_resolve_capabilities_returns_default_when_user_none():
    caps = _resolve_capabilities(None, precise=True)
    assert caps == _default_sensor_capabilities(precise=True)


def test_resolve_capabilities_returns_user_value_when_provided():
    user = StreamCapabilities(
        provides_audio_track=False,
        supports_precise_timestamps=False,
        is_removable=True,
        produces_file=True,
    )
    caps = _resolve_capabilities(user, precise=True)
    assert caps is user
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/adapters/test_generic_internals.py -v`
Expected: 4 new tests fail with `ImportError`.

- [ ] **Step 3: Implement the helpers**

Append to `src/syncfield/adapters/_generic.py`:

```python
def _default_sensor_capabilities(*, precise: bool) -> StreamCapabilities:
    return StreamCapabilities(
        provides_audio_track=False,
        supports_precise_timestamps=precise,
        is_removable=False,
        produces_file=True,
    )


def _resolve_capabilities(
    user: Optional[StreamCapabilities],
    *,
    precise: bool,
) -> StreamCapabilities:
    if user is not None:
        return user
    return _default_sensor_capabilities(precise=precise)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/adapters/test_generic_internals.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/_generic.py tests/unit/adapters/test_generic_internals.py
git commit -m "feat(adapters): capabilities helpers for generic sensor streams"
```

---

## Phase 2 — `PollingSensorStream`

### Task 4: Skeleton with `__init__`, arity detection, capabilities

**Files:**
- Create: `src/syncfield/adapters/polling_sensor.py`
- Create: `tests/unit/adapters/test_polling_sensor.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/adapters/test_polling_sensor.py
"""Unit tests for PollingSensorStream — drive without spawning a thread."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncfield.adapters.polling_sensor import PollingSensorStream
from syncfield.types import StreamCapabilities


def test_polling_sensor_minimal_construction(tmp_path: Path):
    def read():
        return {"x": 1.0}

    stream = PollingSensorStream("imu", read=read, hz=100, output_dir=tmp_path)
    assert stream.id == "imu"
    assert stream.kind == "sensor"
    assert stream.capabilities.supports_precise_timestamps is True
    assert stream.capabilities.produces_file is True


def test_polling_sensor_with_open_close(tmp_path: Path):
    def open_dev():
        return {"handle": True}

    def read(handle):
        return {"x": handle["handle"]}

    def close(handle):
        handle["handle"] = False

    stream = PollingSensorStream(
        "env", read=read, open=open_dev, close=close, hz=10, output_dir=tmp_path,
    )
    assert stream.id == "env"


def test_polling_sensor_arity_mismatch_with_open_raises(tmp_path: Path):
    def read():  # zero args, but open is provided
        return {"x": 1}

    def open_dev():
        return None

    with pytest.raises(TypeError, match="read"):
        PollingSensorStream(
            "bad", read=read, open=open_dev, hz=10, output_dir=tmp_path,
        )


def test_polling_sensor_arity_mismatch_without_open_raises(tmp_path: Path):
    def read(handle):  # one arg, but no open
        return {"x": 1}

    with pytest.raises(TypeError, match="read"):
        PollingSensorStream("bad", read=read, hz=10, output_dir=tmp_path)


def test_polling_sensor_user_capabilities_override(tmp_path: Path):
    user_caps = StreamCapabilities(
        provides_audio_track=False,
        supports_precise_timestamps=True,
        is_removable=True,
        produces_file=True,
    )
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=100,
        output_dir=tmp_path, capabilities=user_caps,
    )
    assert stream.capabilities.is_removable is True


def test_polling_sensor_device_key(tmp_path: Path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=100,
        output_dir=tmp_path, device_key=("serial", "/dev/ttyUSB0"),
    )
    assert stream.device_key == ("serial", "/dev/ttyUSB0")


def test_polling_sensor_default_device_key_is_none(tmp_path: Path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=100, output_dir=tmp_path,
    )
    assert stream.device_key is None
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/adapters/test_polling_sensor.py -v`
Expected: 7 tests fail with `ImportError`.

- [ ] **Step 3: Write the skeleton**

```python
# src/syncfield/adapters/polling_sensor.py
"""PollingSensorStream — generic helper for sensors with a read() function.

The helper owns one capture thread between ``connect()`` and
``disconnect()``. The thread calls the user-supplied ``read()`` function
at the configured ``hz``, captures ``time.monotonic_ns()`` immediately
after each read, and routes the resulting sample through the standard
:meth:`StreamBase._emit_sample` callback path. While the orchestrator
is in ``RECORDING`` the same thread also persists the sample to
``{stream_id}.jsonl`` via the shared :class:`_SensorWriteCore`.

Lifecycle
---------

1. ``connect()`` — call ``open()`` (if provided), spawn the capture thread.
2. ``start_recording(clock)`` — open the writer, flip ``_writing`` on.
3. ``stop_recording()`` — flip ``_writing`` off, close the writer, return
   a :class:`FinalizationReport`.
4. ``disconnect()`` — stop the capture thread, call ``close()`` (if provided).

The capture thread keeps running across ``stop_recording()`` so live
preview during ``CONNECTED`` continues to fire ``on_sample`` callbacks
without disk writes.
"""

from __future__ import annotations

import inspect
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from syncfield.adapters._generic import (
    _SensorWriteCore,
    _resolve_capabilities,
)
from syncfield.clock import SessionClock
from syncfield.stream import DeviceKey, StreamBase
from syncfield.types import (
    ChannelValue,
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    SensorSample,
    StreamCapabilities,
)


class PollingSensorStream(StreamBase):
    """Generic helper that polls a user ``read()`` function on a fixed ``hz``."""

    def __init__(
        self,
        id: str,
        *,
        read: Callable[..., dict[str, ChannelValue]],
        hz: float,
        output_dir: Path | str,
        open: Optional[Callable[[], Any]] = None,
        close: Optional[Callable[[Any], None]] = None,
        device_key: Optional[DeviceKey] = None,
        capabilities: Optional[StreamCapabilities] = None,
        on_read_error: Literal["drop", "stop"] = "drop",
    ) -> None:
        super().__init__(
            id=id,
            kind="sensor",
            capabilities=_resolve_capabilities(capabilities, precise=True),
        )
        self._validate_arity(read, expects_handle=open is not None)
        self._read = read
        self._open = open
        self._close = close
        self._hz = hz
        self._period = 1.0 / hz
        self._on_read_error = on_read_error
        self._device_key = device_key

        self._write_core = _SensorWriteCore(id, Path(output_dir))
        self._handle: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._writing = False

    @staticmethod
    def _validate_arity(
        read: Callable[..., dict[str, ChannelValue]],
        *,
        expects_handle: bool,
    ) -> None:
        sig = inspect.signature(read)
        n_params = len(
            [p for p in sig.parameters.values()
             if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        )
        if expects_handle and n_params != 1:
            raise TypeError(
                f"read must accept exactly 1 argument when open is provided "
                f"(got {n_params})"
            )
        if not expects_handle and n_params != 0:
            raise TypeError(
                f"read must accept 0 arguments when open is not provided "
                f"(got {n_params})"
            )

    @property
    def device_key(self) -> Optional[DeviceKey]:
        return self._device_key
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/adapters/test_polling_sensor.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/polling_sensor.py tests/unit/adapters/test_polling_sensor.py
git commit -m "feat(adapters): PollingSensorStream skeleton (init + arity detection)"
```

---

### Task 5: `_capture_once()` happy path (emits, no disk yet)

**Files:**
- Modify: `src/syncfield/adapters/polling_sensor.py`
- Modify: `tests/unit/adapters/test_polling_sensor.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/unit/adapters/test_polling_sensor.py`:

```python
from syncfield.types import SampleEvent


def test_capture_once_emits_sample_with_channels(tmp_path):
    samples: list[SampleEvent] = []

    def read():
        return {"ax": 0.5, "ay": -0.3}

    stream = PollingSensorStream("imu", read=read, hz=100, output_dir=tmp_path)
    stream.on_sample(samples.append)

    cont = stream._capture_once()
    assert cont is True
    assert len(samples) == 1
    assert samples[0].stream_id == "imu"
    assert samples[0].frame_number == 0
    assert samples[0].channels == {"ax": 0.5, "ay": -0.3}
    assert samples[0].capture_ns > 0


def test_capture_once_increments_frame_number(tmp_path):
    samples: list[SampleEvent] = []
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=100, output_dir=tmp_path,
    )
    stream.on_sample(samples.append)

    for _ in range(5):
        stream._capture_once()

    assert [s.frame_number for s in samples] == [0, 1, 2, 3, 4]


def test_capture_once_passes_handle_when_open_provided(tmp_path):
    samples: list[SampleEvent] = []

    def read(handle):
        return {"value": handle["v"]}

    stream = PollingSensorStream(
        "x", read=read, open=lambda: {"v": 42},
        hz=100, output_dir=tmp_path,
    )
    stream._handle = {"v": 42}  # bypass connect() for unit test
    stream.on_sample(samples.append)

    stream._capture_once()
    assert samples[0].channels == {"value": 42}
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/adapters/test_polling_sensor.py::test_capture_once_emits_sample_with_channels -v`
Expected: `AttributeError: 'PollingSensorStream' object has no attribute '_capture_once'`.

- [ ] **Step 3: Implement `_capture_once()` (no disk path yet)**

Add inside `PollingSensorStream`:

```python
    def _capture_once(self) -> bool:
        """One iteration of the capture loop. Returns False to halt the loop."""
        loop_start = time.monotonic()
        try:
            channels = self._read(self._handle) if self._open else self._read()
        except Exception as exc:
            self._emit_health(HealthEvent(
                self.id, HealthEventKind.ERROR,
                time.monotonic_ns(), str(exc),
            ))
            if self._on_read_error == "stop":
                return False
            time.sleep(self._period)
            return True

        capture_ns = time.monotonic_ns()
        frame_number = self._write_core.next_frame_number()
        self._emit_sample(SampleEvent(
            stream_id=self.id,
            frame_number=frame_number,
            capture_ns=capture_ns,
            channels=channels,
        ))

        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, self._period - elapsed))
        return True
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/adapters/test_polling_sensor.py -v`
Expected: 10 passed (7 from Task 4 + 3 new). The `time.sleep` makes each test add ~`_period` seconds of latency — for `hz=100` that's 10ms per `_capture_once`, acceptable.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/polling_sensor.py tests/unit/adapters/test_polling_sensor.py
git commit -m "feat(adapters): PollingSensorStream._capture_once happy path (emit only)"
```

---

### Task 6: `_capture_once()` writes to disk when `_writing=True`

**Files:**
- Modify: `src/syncfield/adapters/polling_sensor.py`
- Modify: `tests/unit/adapters/test_polling_sensor.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/unit/adapters/test_polling_sensor.py`:

```python
import json


def test_capture_once_does_not_write_when_not_recording(tmp_path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=100, output_dir=tmp_path,
    )
    for _ in range(3):
        stream._capture_once()
    # _writing is False; jsonl file should not exist
    assert not (tmp_path / "imu.jsonl").exists()


def test_capture_once_writes_when_recording(tmp_path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1, "y": 2}, hz=100, output_dir=tmp_path,
    )
    stream._write_core.open()
    stream._writing = True

    for _ in range(3):
        stream._capture_once()

    stream._write_core.close()

    lines = (tmp_path / "imu.jsonl").read_text().strip().split("\n")
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["frame_number"] == 0
    assert first["channels"] == {"x": 1, "y": 2}


def test_capture_once_frame_counter_continuous_across_recording_toggle(tmp_path):
    """Frame counter is monotonic — preview samples advance it too."""
    samples: list = []
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=100, output_dir=tmp_path,
    )
    stream.on_sample(samples.append)

    # Preview phase
    stream._capture_once()  # frame 0
    stream._capture_once()  # frame 1

    # Start recording
    stream._write_core.open()
    stream._writing = True
    stream._capture_once()  # frame 2 (first written)
    stream._capture_once()  # frame 3
    stream._write_core.close()

    lines = (tmp_path / "imu.jsonl").read_text().strip().split("\n")
    written = [json.loads(l) for l in lines]
    assert [w["frame_number"] for w in written] == [2, 3]
    assert [s.frame_number for s in samples] == [0, 1, 2, 3]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/adapters/test_polling_sensor.py::test_capture_once_writes_when_recording -v`
Expected: file does not exist — write path not implemented.

- [ ] **Step 3: Add the write branch**

In `PollingSensorStream._capture_once()`, after the `_emit_sample(...)` call and **before** the `elapsed = ...` line, insert:

```python
        if self._writing:
            try:
                self._write_core.write(SensorSample(
                    frame_number=frame_number,
                    capture_ns=capture_ns,
                    channels=channels,
                ))
            except Exception as exc:
                self._emit_health(HealthEvent(
                    self.id, HealthEventKind.ERROR,
                    time.monotonic_ns(),
                    f"sensor write failed: {exc}",
                ))
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/adapters/test_polling_sensor.py -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/polling_sensor.py tests/unit/adapters/test_polling_sensor.py
git commit -m "feat(adapters): PollingSensorStream writes to jsonl when recording"
```

---

### Task 7: `_capture_once()` error handling (read raises, returns wrong type)

**Files:**
- Modify: `src/syncfield/adapters/polling_sensor.py`
- Modify: `tests/unit/adapters/test_polling_sensor.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/unit/adapters/test_polling_sensor.py`:

```python
from syncfield.types import HealthEventKind


def test_capture_once_drop_on_read_error_default(tmp_path):
    health: list = []
    calls = {"n": 0}

    def read():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("device disconnected")
        return {"x": 1}

    stream = PollingSensorStream("imu", read=read, hz=100, output_dir=tmp_path)
    stream.on_health(health.append)

    cont1 = stream._capture_once()  # raises, drops
    cont2 = stream._capture_once()  # succeeds
    assert cont1 is True
    assert cont2 is True
    assert len(health) == 1
    assert health[0].kind == HealthEventKind.ERROR
    assert "device disconnected" in (health[0].detail or "")


def test_capture_once_stop_on_read_error_when_configured(tmp_path):
    def read():
        raise RuntimeError("permanent failure")

    stream = PollingSensorStream(
        "imu", read=read, hz=100, output_dir=tmp_path,
        on_read_error="stop",
    )
    cont = stream._capture_once()
    assert cont is False


def test_capture_once_drop_on_non_dict_return(tmp_path):
    health: list = []
    calls = {"n": 0}

    def read():
        calls["n"] += 1
        if calls["n"] == 1:
            return [1, 2, 3]  # wrong type
        return {"x": 1}

    stream = PollingSensorStream("imu", read=read, hz=100, output_dir=tmp_path)
    stream.on_health(health.append)

    cont1 = stream._capture_once()
    cont2 = stream._capture_once()
    assert cont1 is True
    assert cont2 is True
    assert len(health) == 1
    assert health[0].kind == HealthEventKind.ERROR
    assert "list" in (health[0].detail or "")


def test_capture_once_stop_on_non_dict_return_when_configured(tmp_path):
    stream = PollingSensorStream(
        "imu", read=lambda: "nope", hz=100,
        output_dir=tmp_path, on_read_error="stop",
    )
    assert stream._capture_once() is False
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/adapters/test_polling_sensor.py::test_capture_once_drop_on_non_dict_return -v`
Expected: `assert len(health) == 1` fails (no type-check yet); the test that follows hangs/passes incorrectly.

- [ ] **Step 3: Add the type check**

In `PollingSensorStream._capture_once()`, **after** the `try/except` that calls `self._read(...)` and **before** `capture_ns = time.monotonic_ns()`, insert:

```python
        if not isinstance(channels, dict):
            self._emit_health(HealthEvent(
                self.id, HealthEventKind.ERROR,
                time.monotonic_ns(),
                f"read() returned {type(channels).__name__}, expected dict",
            ))
            if self._on_read_error == "stop":
                return False
            time.sleep(self._period)
            return True
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/adapters/test_polling_sensor.py -v`
Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/polling_sensor.py tests/unit/adapters/test_polling_sensor.py
git commit -m "feat(adapters): PollingSensorStream error handling (drop|stop)"
```

---

### Task 8: 4-phase lifecycle (connect spawns thread, disconnect joins, start/stop_recording wires writer)

**Files:**
- Modify: `src/syncfield/adapters/polling_sensor.py`
- Modify: `tests/unit/adapters/test_polling_sensor.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/unit/adapters/test_polling_sensor.py`:

```python
from syncfield.clock import SessionClock
from syncfield.types import SyncPoint


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


def test_connect_calls_open_and_spawns_thread(tmp_path):
    opened = {"called": False}

    def open_dev():
        opened["called"] = True
        return {"port": "/dev/ttyX"}

    def read(handle):
        return {"x": 1}

    def close(handle):
        pass

    stream = PollingSensorStream(
        "imu", read=read, open=open_dev, close=close,
        hz=1000, output_dir=tmp_path,
    )
    stream.connect()
    try:
        assert opened["called"] is True
        assert stream._handle == {"port": "/dev/ttyX"}
        assert stream._thread is not None
        assert stream._thread.is_alive()
    finally:
        stream.disconnect()


def test_disconnect_joins_thread_and_calls_close(tmp_path):
    closed = {"called": False}

    def close(handle):
        closed["called"] = True

    stream = PollingSensorStream(
        "imu", read=lambda h: {"x": 1},
        open=lambda: "h", close=close,
        hz=1000, output_dir=tmp_path,
    )
    stream.connect()
    stream.disconnect()
    assert closed["called"] is True
    assert stream._handle is None
    assert stream._thread is not None  # we keep the reference
    assert not stream._thread.is_alive()


def test_disconnect_without_close_callback(tmp_path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=1000, output_dir=tmp_path,
    )
    stream.connect()
    stream.disconnect()  # must not raise


def test_open_raises_bubbles_out_of_connect(tmp_path):
    def open_dev():
        raise RuntimeError("permission denied")

    stream = PollingSensorStream(
        "imu", read=lambda h: {"x": 1}, open=open_dev,
        hz=100, output_dir=tmp_path,
    )
    with pytest.raises(RuntimeError, match="permission denied"):
        stream.connect()


def test_close_raises_emits_warning_but_disconnect_succeeds(tmp_path):
    health: list = []

    def close(handle):
        raise RuntimeError("close failed")

    stream = PollingSensorStream(
        "imu", read=lambda h: {"x": 1},
        open=lambda: "h", close=close,
        hz=1000, output_dir=tmp_path,
    )
    stream.on_health(health.append)
    stream.connect()
    stream.disconnect()  # must not raise
    warnings = [h for h in health if h.kind == HealthEventKind.WARNING]
    assert any("close failed" in (w.detail or "") for w in warnings)


def test_start_recording_opens_writer_and_flips_writing(tmp_path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=1000, output_dir=tmp_path,
    )
    stream.connect()
    try:
        stream.start_recording(_clock())
        assert stream._writing is True
        assert (tmp_path / "imu.jsonl").exists()
    finally:
        stream.stop_recording()
        stream.disconnect()


def test_stop_recording_returns_finalization_report(tmp_path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=1000, output_dir=tmp_path,
    )
    stream.connect()
    stream.start_recording(_clock())
    time.sleep(0.05)  # let the thread record a handful of samples
    report = stream.stop_recording()
    stream.disconnect()

    assert report.stream_id == "imu"
    assert report.status == "completed"
    assert report.frame_count > 0
    assert report.file_path == tmp_path / "imu.jsonl"
    assert report.first_sample_at_ns is not None
    assert report.last_sample_at_ns is not None
    assert report.last_sample_at_ns >= report.first_sample_at_ns
    assert report.error is None


import time as _time_for_sleep_import  # already imported above? sentinel  # noqa
```

(Note: `time` is already imported by the polling helper but the test file needs its own import. Add `import time` at the top of the test file if not already present.)

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/adapters/test_polling_sensor.py::test_connect_calls_open_and_spawns_thread -v`
Expected: `connect()` is the inherited no-op from `StreamBase`, no thread spawned.

- [ ] **Step 3: Implement the lifecycle**

Add to `PollingSensorStream`:

```python
    def _capture_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._capture_once():
                return

    def connect(self) -> None:
        if self._open is not None:
            self._handle = self._open()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop,
            name=f"polling-sensor-{self.id}",
            daemon=True,
        )
        self._thread.start()

    def start_recording(self, session_clock: SessionClock) -> None:
        self._write_core.open()
        self._writing = True

    def stop_recording(self) -> FinalizationReport:
        self._writing = False
        self._write_core.close()
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=self._write_core.frame_count,
            file_path=self._write_core.path,
            first_sample_at_ns=self._write_core.first_sample_at_ns,
            last_sample_at_ns=self._write_core.last_sample_at_ns,
            health_events=list(self._collected_health),
            error=None,
        )

    def disconnect(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                self._emit_health(HealthEvent(
                    self.id, HealthEventKind.WARNING,
                    time.monotonic_ns(),
                    "capture thread did not exit within 3s",
                ))
        if self._close is not None and self._handle is not None:
            try:
                self._close(self._handle)
            except Exception as exc:
                self._emit_health(HealthEvent(
                    self.id, HealthEventKind.WARNING,
                    time.monotonic_ns(), f"close failed: {exc}",
                ))
        self._handle = None
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/adapters/test_polling_sensor.py -v`
Expected: 24 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/polling_sensor.py tests/unit/adapters/test_polling_sensor.py
git commit -m "feat(adapters): PollingSensorStream 4-phase lifecycle + finalization"
```

---

### Task 9: Re-export `PollingSensorStream` from `adapters/__init__.py`

**Files:**
- Modify: `src/syncfield/adapters/__init__.py`
- Modify: `tests/unit/test_public_api.py` (if it tracks `__all__`)

- [ ] **Step 1: Inspect existing public-API test**

Run: `pytest tests/unit/test_public_api.py -v`
If the test uses `from syncfield.adapters import PollingSensorStream` style assertions, plan to extend it. Otherwise just verify the re-export works manually.

- [ ] **Step 2: Add a small failing test**

```python
# tests/unit/adapters/test_polling_sensor.py — append
def test_polling_sensor_stream_is_re_exported_from_adapters_package():
    from syncfield.adapters import PollingSensorStream as Reexported
    assert Reexported is PollingSensorStream
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/unit/adapters/test_polling_sensor.py::test_polling_sensor_stream_is_re_exported_from_adapters_package -v`
Expected: `ImportError: cannot import name 'PollingSensorStream'`.

- [ ] **Step 4: Add the re-export**

In `src/syncfield/adapters/__init__.py`, after the `from syncfield.adapters.jsonl_file import JSONLFileStream` line, add:

```python
from syncfield.adapters.polling_sensor import PollingSensorStream
```

And update `__all__`:

```python
__all__ = ["JSONLFileStream", "PollingSensorStream"]
```

- [ ] **Step 5: Run to verify pass + commit**

Run: `pytest tests/unit/adapters/test_polling_sensor.py -v`
Expected: 25 passed.

```bash
git add src/syncfield/adapters/__init__.py tests/unit/adapters/test_polling_sensor.py
git commit -m "feat(adapters): re-export PollingSensorStream from adapters package"
```

---

## Phase 3 — `PushSensorStream`

### Task 10: Skeleton with `__init__`, capabilities, device_key

**Files:**
- Create: `src/syncfield/adapters/push_sensor.py`
- Create: `tests/unit/adapters/test_push_sensor.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/adapters/test_push_sensor.py
"""Unit tests for PushSensorStream."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncfield.adapters.push_sensor import PushSensorStream
from syncfield.types import StreamCapabilities


def test_push_sensor_minimal_construction(tmp_path: Path):
    stream = PushSensorStream("ble_imu", output_dir=tmp_path)
    assert stream.id == "ble_imu"
    assert stream.kind == "sensor"
    assert stream.capabilities.supports_precise_timestamps is False
    assert stream.capabilities.produces_file is True


def test_push_sensor_user_capabilities_override(tmp_path: Path):
    user = StreamCapabilities(
        provides_audio_track=False,
        supports_precise_timestamps=True,
        is_removable=True,
        produces_file=True,
    )
    stream = PushSensorStream("ble", output_dir=tmp_path, capabilities=user)
    assert stream.capabilities.is_removable is True
    assert stream.capabilities.supports_precise_timestamps is True


def test_push_sensor_device_key(tmp_path: Path):
    stream = PushSensorStream(
        "ble", output_dir=tmp_path, device_key=("ble", "AA:BB:CC:DD:EE:FF"),
    )
    assert stream.device_key == ("ble", "AA:BB:CC:DD:EE:FF")


def test_push_sensor_default_device_key_is_none(tmp_path: Path):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    assert stream.device_key is None
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/adapters/test_push_sensor.py -v`
Expected: 4 tests fail with `ImportError`.

- [ ] **Step 3: Write the skeleton**

```python
# src/syncfield/adapters/push_sensor.py
"""PushSensorStream — generic helper for callback/asyncio/external-thread sources.

The user owns the producer thread. The helper exposes a thread-safe
``push(channels, capture_ns=None)`` method that the user calls from
inside their callback or task. Optional ``on_connect`` / ``on_disconnect``
hooks let the user start/stop their own loop in lock-step with the
SyncField lifecycle.

While the orchestrator is in ``RECORDING`` the helper persists each
``push()`` to ``{stream_id}.jsonl``. Outside of recording, ``push()``
still emits to ``on_sample`` callbacks (so live preview works) but
performs no disk I/O.

``push()`` is designed to **never raise** for SyncField-internal
failures — the user thread (often a BLE callback) must not be killed
by the SDK. Bad ``channels`` types raise ``TypeError`` because that's
a user-code bug worth surfacing.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Optional

from syncfield.adapters._generic import (
    _SensorWriteCore,
    _resolve_capabilities,
)
from syncfield.clock import SessionClock
from syncfield.stream import DeviceKey, StreamBase
from syncfield.types import (
    ChannelValue,
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    SensorSample,
    StreamCapabilities,
)


class PushSensorStream(StreamBase):
    """Generic helper for sensors driven by user-owned producer threads."""

    def __init__(
        self,
        id: str,
        *,
        output_dir: Path | str,
        on_connect: Optional[Callable[["PushSensorStream"], None]] = None,
        on_disconnect: Optional[Callable[["PushSensorStream"], None]] = None,
        device_key: Optional[DeviceKey] = None,
        capabilities: Optional[StreamCapabilities] = None,
    ) -> None:
        super().__init__(
            id=id,
            kind="sensor",
            capabilities=_resolve_capabilities(capabilities, precise=False),
        )
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._device_key = device_key

        self._write_core = _SensorWriteCore(id, Path(output_dir))
        self._push_lock = threading.Lock()
        self._connected = False
        self._writing = False

    @property
    def device_key(self) -> Optional[DeviceKey]:
        return self._device_key
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/adapters/test_push_sensor.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/push_sensor.py tests/unit/adapters/test_push_sensor.py
git commit -m "feat(adapters): PushSensorStream skeleton"
```

---

### Task 11: 4-phase lifecycle (connect/disconnect callbacks, start/stop_recording)

**Files:**
- Modify: `src/syncfield/adapters/push_sensor.py`
- Modify: `tests/unit/adapters/test_push_sensor.py`

- [ ] **Step 1: Add the failing tests**

```python
# tests/unit/adapters/test_push_sensor.py — append
from syncfield.clock import SessionClock
from syncfield.types import SyncPoint


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


def test_connect_invokes_on_connect_callback(tmp_path):
    captured = {"stream": None}

    def on_connect(stream):
        captured["stream"] = stream

    stream = PushSensorStream("ble", output_dir=tmp_path, on_connect=on_connect)
    stream.connect()
    assert captured["stream"] is stream
    assert stream._connected is True
    stream.disconnect()


def test_disconnect_invokes_on_disconnect_callback(tmp_path):
    captured = {"stream": None}

    def on_disconnect(stream):
        captured["stream"] = stream

    stream = PushSensorStream("ble", output_dir=tmp_path, on_disconnect=on_disconnect)
    stream.connect()
    stream.disconnect()
    assert captured["stream"] is stream
    assert stream._connected is False


def test_lifecycle_without_callbacks(tmp_path):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    stream.start_recording(_clock())
    report = stream.stop_recording()
    stream.disconnect()
    assert report.status == "completed"
    assert report.frame_count == 0


def test_start_recording_opens_writer(tmp_path):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    stream.start_recording(_clock())
    assert stream._writing is True
    assert (tmp_path / "ble.jsonl").exists()
    stream.stop_recording()
    stream.disconnect()


def test_stop_recording_returns_finalization_report(tmp_path):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    stream.start_recording(_clock())
    report = stream.stop_recording()
    stream.disconnect()
    assert report.stream_id == "ble"
    assert report.file_path == tmp_path / "ble.jsonl"
    assert report.error is None
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/adapters/test_push_sensor.py::test_connect_invokes_on_connect_callback -v`
Expected: `assert stream._connected is True` fails — `connect()` is the inherited no-op.

- [ ] **Step 3: Implement the lifecycle methods**

Add to `PushSensorStream`:

```python
    def connect(self) -> None:
        self._connected = True
        if self._on_connect is not None:
            self._on_connect(self)

    def start_recording(self, session_clock: SessionClock) -> None:
        self._write_core.open()
        self._writing = True

    def stop_recording(self) -> FinalizationReport:
        self._writing = False
        self._write_core.close()
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=self._write_core.frame_count,
            file_path=self._write_core.path,
            first_sample_at_ns=self._write_core.first_sample_at_ns,
            last_sample_at_ns=self._write_core.last_sample_at_ns,
            health_events=list(self._collected_health),
            error=None,
        )

    def disconnect(self) -> None:
        if self._on_disconnect is not None:
            try:
                self._on_disconnect(self)
            except Exception as exc:
                self._emit_health(HealthEvent(
                    self.id, HealthEventKind.WARNING,
                    time.monotonic_ns(),
                    f"on_disconnect raised: {exc}",
                ))
        self._connected = False
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/adapters/test_push_sensor.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/push_sensor.py tests/unit/adapters/test_push_sensor.py
git commit -m "feat(adapters): PushSensorStream 4-phase lifecycle"
```

---

### Task 12: `push()` happy path (with disk write when recording)

**Files:**
- Modify: `src/syncfield/adapters/push_sensor.py`
- Modify: `tests/unit/adapters/test_push_sensor.py`

- [ ] **Step 1: Add the failing tests**

```python
# tests/unit/adapters/test_push_sensor.py — append
import json

from syncfield.types import SampleEvent


def test_push_emits_sample_when_connected(tmp_path):
    samples: list[SampleEvent] = []
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.on_sample(samples.append)
    stream.connect()
    stream.push({"ax": 0.5})
    assert len(samples) == 1
    assert samples[0].channels == {"ax": 0.5}
    assert samples[0].frame_number == 0
    assert samples[0].capture_ns > 0
    stream.disconnect()


def test_push_default_capture_ns_uses_monotonic_now(tmp_path):
    samples: list[SampleEvent] = []
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.on_sample(samples.append)
    stream.connect()

    before = time.monotonic_ns()
    stream.push({"x": 1})
    after = time.monotonic_ns()

    assert before <= samples[0].capture_ns <= after
    stream.disconnect()


def test_push_explicit_capture_ns_preserved(tmp_path):
    samples: list[SampleEvent] = []
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.on_sample(samples.append)
    stream.connect()
    stream.push({"x": 1}, capture_ns=1234567890)
    assert samples[0].capture_ns == 1234567890
    stream.disconnect()


def test_push_explicit_frame_number_preserved(tmp_path):
    samples: list[SampleEvent] = []
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.on_sample(samples.append)
    stream.connect()
    stream.push({"x": 1}, frame_number=42)
    assert samples[0].frame_number == 42
    stream.disconnect()


def test_push_does_not_write_outside_recording(tmp_path):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    stream.push({"x": 1})
    stream.push({"x": 2})
    stream.disconnect()
    assert not (tmp_path / "ble.jsonl").exists()


def test_push_writes_when_recording(tmp_path):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    stream.start_recording(_clock())
    stream.push({"x": 1})
    stream.push({"x": 2})
    stream.push({"x": 3})
    stream.stop_recording()
    stream.disconnect()

    lines = (tmp_path / "ble.jsonl").read_text().strip().split("\n")
    assert len(lines) == 3
    assert [json.loads(l)["channels"]["x"] for l in lines] == [1, 2, 3]


def test_push_frame_counter_continuous_across_recording_toggle(tmp_path):
    samples: list[SampleEvent] = []
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.on_sample(samples.append)

    stream.connect()
    stream.push({"x": 1})  # frame 0 (preview)
    stream.push({"x": 2})  # frame 1 (preview)

    stream.start_recording(_clock())
    stream.push({"x": 3})  # frame 2 (written)
    stream.push({"x": 4})  # frame 3 (written)
    stream.stop_recording()
    stream.disconnect()

    lines = (tmp_path / "ble.jsonl").read_text().strip().split("\n")
    written = [json.loads(l) for l in lines]
    assert [w["frame_number"] for w in written] == [2, 3]
    assert [s.frame_number for s in samples] == [0, 1, 2, 3]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/adapters/test_push_sensor.py::test_push_emits_sample_when_connected -v`
Expected: `AttributeError: 'PushSensorStream' object has no attribute 'push'`.

- [ ] **Step 3: Implement `push()` (happy path only)**

Add to `PushSensorStream`:

```python
    def push(
        self,
        channels: dict[str, ChannelValue],
        *,
        capture_ns: Optional[int] = None,
        frame_number: Optional[int] = None,
    ) -> None:
        if capture_ns is None:
            capture_ns = time.monotonic_ns()
        with self._push_lock:
            if frame_number is None:
                frame_number = self._write_core.next_frame_number()
            self._emit_sample(SampleEvent(
                stream_id=self.id,
                frame_number=frame_number,
                capture_ns=capture_ns,
                channels=channels,
            ))
            if self._writing:
                try:
                    self._write_core.write(SensorSample(
                        frame_number=frame_number,
                        capture_ns=capture_ns,
                        channels=channels,
                    ))
                except Exception as exc:
                    self._emit_health(HealthEvent(
                        self.id, HealthEventKind.ERROR,
                        time.monotonic_ns(),
                        f"sensor write failed: {exc}",
                    ))
```

Also add `import time` at the top of the file if not already present (it is — see Task 10 skeleton).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/adapters/test_push_sensor.py -v`
Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/push_sensor.py tests/unit/adapters/test_push_sensor.py
git commit -m "feat(adapters): PushSensorStream.push() happy path"
```

---

### Task 13: `push()` error handling (not connected, bad type)

**Files:**
- Modify: `src/syncfield/adapters/push_sensor.py`
- Modify: `tests/unit/adapters/test_push_sensor.py`

- [ ] **Step 1: Add the failing tests**

```python
# tests/unit/adapters/test_push_sensor.py — append
from syncfield.types import HealthEventKind


def test_push_before_connect_drops_with_warning(tmp_path):
    health: list = []
    samples: list = []
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.on_sample(samples.append)
    stream.on_health(health.append)

    stream.push({"x": 1})  # before connect

    assert samples == []
    warnings = [h for h in health if h.kind == HealthEventKind.WARNING]
    assert len(warnings) == 1
    assert "outside connect/disconnect" in (warnings[0].detail or "")


def test_push_after_disconnect_drops_with_warning(tmp_path):
    health: list = []
    samples: list = []
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.on_sample(samples.append)
    stream.on_health(health.append)

    stream.connect()
    stream.disconnect()
    stream.push({"x": 1})  # after disconnect

    assert samples == []
    warnings = [h for h in health if h.kind == HealthEventKind.WARNING]
    assert len(warnings) == 1


def test_push_with_non_dict_channels_raises_typeerror(tmp_path):
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    with pytest.raises(TypeError, match="dict"):
        stream.push([1, 2, 3])  # type: ignore[arg-type]
    stream.disconnect()


def test_push_never_raises_for_internal_failures(tmp_path, monkeypatch):
    """Even if the writer blows up mid-recording, push() must not raise."""
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    stream.start_recording(_clock())

    # Force the writer to raise on every write
    def boom(_sample):
        raise OSError("disk full")

    monkeypatch.setattr(stream._write_core, "write", boom)

    health: list = []
    stream.on_health(health.append)
    stream.push({"x": 1})  # must not raise

    errors = [h for h in health if h.kind == HealthEventKind.ERROR]
    assert any("disk full" in (e.detail or "") for e in errors)

    stream.stop_recording()
    stream.disconnect()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/adapters/test_push_sensor.py::test_push_before_connect_drops_with_warning -v`
Expected: `len(samples) == 1` (push currently emits regardless of state).

- [ ] **Step 3: Add the guards**

Modify `PushSensorStream.push()` so the **first** lines become:

```python
    def push(
        self,
        channels: dict[str, ChannelValue],
        *,
        capture_ns: Optional[int] = None,
        frame_number: Optional[int] = None,
    ) -> None:
        if not isinstance(channels, dict):
            raise TypeError(
                f"PushSensorStream.push: channels must be dict, "
                f"got {type(channels).__name__}"
            )
        if not self._connected:
            self._emit_health(HealthEvent(
                self.id, HealthEventKind.WARNING, time.monotonic_ns(),
                "push() called outside connect/disconnect; sample dropped",
            ))
            return
        if capture_ns is None:
            capture_ns = time.monotonic_ns()
        # ... rest of the body unchanged
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/adapters/test_push_sensor.py -v`
Expected: 20 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/push_sensor.py tests/unit/adapters/test_push_sensor.py
git commit -m "feat(adapters): PushSensorStream.push() error handling"
```

---

### Task 14: Re-export `PushSensorStream` from `adapters/__init__.py`

**Files:**
- Modify: `src/syncfield/adapters/__init__.py`
- Modify: `tests/unit/adapters/test_push_sensor.py`

- [ ] **Step 1: Add the failing test**

```python
# tests/unit/adapters/test_push_sensor.py — append
def test_push_sensor_stream_is_re_exported_from_adapters_package():
    from syncfield.adapters import PushSensorStream as Reexported
    assert Reexported is PushSensorStream
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/adapters/test_push_sensor.py::test_push_sensor_stream_is_re_exported_from_adapters_package -v`
Expected: `ImportError: cannot import name 'PushSensorStream'`.

- [ ] **Step 3: Add the re-export**

In `src/syncfield/adapters/__init__.py`, after the `from syncfield.adapters.polling_sensor import PollingSensorStream` line added in Task 9, add:

```python
from syncfield.adapters.push_sensor import PushSensorStream
```

And update `__all__`:

```python
__all__ = ["JSONLFileStream", "PollingSensorStream", "PushSensorStream"]
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/adapters/test_push_sensor.py -v`
Expected: 21 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/__init__.py tests/unit/adapters/test_push_sensor.py
git commit -m "feat(adapters): re-export PushSensorStream from adapters package"
```

---

## Phase 4 — Threading Integration Tests

### Task 15: PollingSensorStream real-thread integration test

**Files:**
- Create: `tests/integration/adapters/__init__.py` (if missing)
- Create: `tests/integration/adapters/test_polling_sensor_threading.py`

- [ ] **Step 1: Ensure the integration package marker exists**

Run: `ls tests/integration/adapters/__init__.py 2>/dev/null || echo MISSING`

If missing, create it:

```python
# tests/integration/adapters/__init__.py
```

- [ ] **Step 2: Write the test**

```python
# tests/integration/adapters/test_polling_sensor_threading.py
"""Real-thread integration test for PollingSensorStream.

These tests spin up the actual capture thread and assert on observed
sample counts. They use generous tolerances (±30%) because wallclock
sleeps are unreliable on busy CI runners. If they prove flaky we'll
switch to a deterministic clock injection.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from syncfield.adapters.polling_sensor import PollingSensorStream
from syncfield.clock import SessionClock
from syncfield.types import SyncPoint


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


def test_real_thread_records_expected_sample_count(tmp_path: Path):
    counter = {"n": 0}

    def read():
        counter["n"] += 1
        return {"i": counter["n"]}

    stream = PollingSensorStream(
        "imu", read=read, hz=200, output_dir=tmp_path,
    )
    stream.connect()
    try:
        stream.start_recording(_clock())
        time.sleep(0.5)  # ~100 samples expected at 200 Hz
        report = stream.stop_recording()
    finally:
        stream.disconnect()

    assert report.status == "completed"
    # 200 Hz × 0.5s = 100 samples; allow ±30% slack for scheduler jitter
    assert 70 <= report.frame_count <= 130, (
        f"expected ~100 samples, got {report.frame_count}"
    )

    lines = (tmp_path / "imu.jsonl").read_text().strip().split("\n")
    assert len(lines) == report.frame_count
    # frame numbers within the recorded slice are monotonic
    written = [json.loads(l) for l in lines]
    fnums = [w["frame_number"] for w in written]
    assert fnums == sorted(fnums)
    # capture_ns is monotonic too
    caps = [w["capture_ns"] for w in written]
    assert caps == sorted(caps)


def test_thread_keeps_running_across_record_cycles(tmp_path: Path):
    """Capture thread stays alive across stop_recording → start_recording."""
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=200, output_dir=tmp_path,
    )
    stream.connect()
    try:
        stream.start_recording(_clock())
        time.sleep(0.2)
        report1 = stream.stop_recording()

        # Thread is still alive — capture preview during this gap
        assert stream._thread is not None and stream._thread.is_alive()
        time.sleep(0.1)

        # Second recording cycle reuses the same thread
        # Need a new write core because the previous one was closed
        stream._write_core = type(stream._write_core)("imu", tmp_path)
        stream.start_recording(_clock())
        time.sleep(0.2)
        report2 = stream.stop_recording()
    finally:
        stream.disconnect()

    assert report1.frame_count > 0
    assert report2.frame_count > 0


def test_disconnect_joins_thread_within_timeout(tmp_path: Path):
    stream = PollingSensorStream(
        "imu", read=lambda: {"x": 1}, hz=1000, output_dir=tmp_path,
    )
    stream.connect()
    t0 = time.monotonic()
    stream.disconnect()
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"disconnect took {elapsed:.3f}s, expected <1s"
    assert not stream._thread.is_alive()
```

**Note on `test_thread_keeps_running_across_record_cycles`:** The test
allocates a fresh `_SensorWriteCore` for the second recording cycle
because the first `stop_recording()` closed the writer. In real
usage the orchestrator handles this: each session runs `start_recording`
once. We will revisit "multi-record-per-connect" semantics in the
e2e task — for now this test documents the current behavior.

- [ ] **Step 3: Run the tests**

Run: `pytest tests/integration/adapters/test_polling_sensor_threading.py -v`
Expected: 3 passed (with timing variance — re-run on flake before debugging).

- [ ] **Step 4: Commit**

```bash
git add tests/integration/adapters/__init__.py tests/integration/adapters/test_polling_sensor_threading.py
git commit -m "test(adapters): PollingSensorStream threading integration test"
```

---

### Task 16: PushSensorStream multi-producer stress test

**Files:**
- Create: `tests/integration/adapters/test_push_sensor_threading.py`

- [ ] **Step 1: Write the test**

```python
# tests/integration/adapters/test_push_sensor_threading.py
"""Multi-producer stress test for PushSensorStream."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from syncfield.adapters.push_sensor import PushSensorStream
from syncfield.clock import SessionClock
from syncfield.types import SyncPoint


def _clock() -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now("h"))


def test_concurrent_pushers_no_lost_samples_no_torn_writes(tmp_path: Path):
    N_THREADS = 50
    PUSHES_PER_THREAD = 100
    EXPECTED_TOTAL = N_THREADS * PUSHES_PER_THREAD

    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    stream.start_recording(_clock())

    def producer(tid: int) -> None:
        for i in range(PUSHES_PER_THREAD):
            stream.push({"tid": tid, "i": i}, capture_ns=tid * 1_000_000 + i)

    threads = [threading.Thread(target=producer, args=(t,)) for t in range(N_THREADS)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0

    report = stream.stop_recording()
    stream.disconnect()

    assert report.frame_count == EXPECTED_TOTAL, (
        f"expected {EXPECTED_TOTAL}, got {report.frame_count}"
    )

    lines = (tmp_path / "ble.jsonl").read_text().strip().split("\n")
    assert len(lines) == EXPECTED_TOTAL

    # No torn writes — every line is parseable JSON
    parsed = [json.loads(l) for l in lines]

    # Frame numbers are unique and cover [0, EXPECTED_TOTAL)
    fnums = sorted(p["frame_number"] for p in parsed)
    assert fnums == list(range(EXPECTED_TOTAL))

    print(f"  pushed {EXPECTED_TOTAL} samples in {elapsed:.3f}s "
          f"({EXPECTED_TOTAL / elapsed:.0f} samples/sec)")


def test_push_during_stop_recording_does_not_crash(tmp_path: Path):
    """A user thread that pushes right as stop_recording() runs must not crash."""
    stream = PushSensorStream("ble", output_dir=tmp_path)
    stream.connect()
    stream.start_recording(_clock())

    stop_event = threading.Event()
    pushed = [0]

    def producer():
        while not stop_event.is_set():
            try:
                stream.push({"x": pushed[0]})
                pushed[0] += 1
            except Exception as exc:
                stop_event.set()
                raise

    t = threading.Thread(target=producer)
    t.start()
    time.sleep(0.1)

    report = stream.stop_recording()
    # Producer keeps pushing past stop_recording — should be a no-op write
    time.sleep(0.05)
    stop_event.set()
    t.join(timeout=1.0)
    stream.disconnect()

    assert pushed[0] > 0
    assert report.frame_count > 0
    assert report.frame_count <= pushed[0]
```

- [ ] **Step 2: Run the tests**

Run: `pytest tests/integration/adapters/test_push_sensor_threading.py -v -s`
Expected: 2 passed. Watch the printed throughput.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/adapters/test_push_sensor_threading.py
git commit -m "test(adapters): PushSensorStream multi-producer stress test"
```

---

## Phase 5 — End-to-End Test

### Task 17: Full session walk through `SessionOrchestrator`

**Files:**
- Create: `tests/integration/test_generic_sensor_e2e.py`

- [ ] **Step 1: Inspect orchestrator surface for the test**

Run: `pytest tests/unit/test_orchestrator.py::test_full_session_lifecycle -v 2>&1 | head -40`

Goal: confirm the public method names actually exist (`add`, `connect`, `start`, `stop`, `disconnect`). If the orchestrator uses different names, mirror them in the test below.

Read the first ~150 lines of `src/syncfield/orchestrator.py` to confirm the constructor signature and the lifecycle method names. Adjust the test below to match.

- [ ] **Step 2: Write the e2e test**

```python
# tests/integration/test_generic_sensor_e2e.py
"""End-to-end test: SessionOrchestrator + both generic sensor helpers."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from syncfield.adapters.polling_sensor import PollingSensorStream
from syncfield.adapters.push_sensor import PushSensorStream
from syncfield.orchestrator import SessionOrchestrator
from syncfield.tone import SyncToneConfig


def test_orchestrator_with_polling_and_push_helpers(tmp_path: Path):
    session = SessionOrchestrator(
        host_id="e2e_host",
        output_dir=tmp_path,
        sync_tone=SyncToneConfig.silent(),
    )

    # Polling helper — counter sensor
    poll_counter = {"n": 0}

    def read_counter():
        poll_counter["n"] += 1
        return {"i": poll_counter["n"]}

    polling = PollingSensorStream(
        "poll_imu", read=read_counter, hz=100, output_dir=tmp_path,
    )
    session.add(polling)

    # Push helper — driven by a background thread
    push_stream = PushSensorStream("push_imu", output_dir=tmp_path)
    session.add(push_stream)

    push_stop = threading.Event()
    push_count = [0]

    def push_producer():
        while not push_stop.is_set():
            push_stream.push({"v": push_count[0]})
            push_count[0] += 1
            time.sleep(0.01)

    push_thread = threading.Thread(target=push_producer, daemon=True)

    # Walk the lifecycle
    session.connect()
    push_thread.start()
    try:
        session.start()
        time.sleep(0.3)
        report = session.stop()
    finally:
        push_stop.set()
        push_thread.join(timeout=1.0)
        session.disconnect()

    # ── Verify both streams produced data ────────────────────────────────
    finalizations = {f.stream_id: f for f in report.finalizations}
    assert "poll_imu" in finalizations
    assert "push_imu" in finalizations

    poll_fin = finalizations["poll_imu"]
    push_fin = finalizations["push_imu"]
    assert poll_fin.status == "completed"
    assert push_fin.status == "completed"
    assert poll_fin.frame_count > 0
    assert push_fin.frame_count > 0

    # ── Verify the JSONL files ───────────────────────────────────────────
    poll_path = tmp_path / "poll_imu.jsonl"
    push_path = tmp_path / "push_imu.jsonl"
    assert poll_path.exists()
    assert push_path.exists()

    poll_lines = poll_path.read_text().strip().split("\n")
    push_lines = push_path.read_text().strip().split("\n")
    assert len(poll_lines) == poll_fin.frame_count
    assert len(push_lines) == push_fin.frame_count

    # JSONL records match the SensorSample schema
    first_poll = json.loads(poll_lines[0])
    assert "frame_number" in first_poll
    assert "capture_ns" in first_poll
    assert "channels" in first_poll
    assert first_poll["clock_source"] == "host_monotonic"

    # ── Verify the manifest mentions both streams with capabilities ──────
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert "poll_imu" in manifest["streams"]
    assert "push_imu" in manifest["streams"]
```

**Note:** if `SessionOrchestrator(host_id=..., output_dir=..., sync_tone=...)` does not match the actual constructor signature you find in Step 1, adjust the kwargs accordingly. The orchestrator may also write the manifest under a different key — inspect the actual `manifest.json` produced by an existing test like `tests/integration/test_round_trip.py` to confirm the structure.

- [ ] **Step 3: Run the test**

Run: `pytest tests/integration/test_generic_sensor_e2e.py -v -s`
Expected: 1 passed. If anything fails, the most likely cause is a mismatched orchestrator API — fix the test, not the helpers.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_generic_sensor_e2e.py
git commit -m "test(adapters): generic sensor helpers e2e through SessionOrchestrator"
```

---

## Phase 6 — Examples

### Task 18: Polling helper example (fake serial source)

**Files:**
- Create: `examples/generic_sensor_demo/README.md`
- Create: `examples/generic_sensor_demo/polling_serial.py`

- [ ] **Step 1: Verify the directory does not exist**

Run: `ls examples/generic_sensor_demo 2>/dev/null && echo EXISTS || echo OK`
Expected: `OK`.

- [ ] **Step 2: Write the README**

```markdown
<!-- examples/generic_sensor_demo/README.md -->
# Generic Sensor Helpers — Demo

Two minimal recipes that show how to attach a sensor to a SyncField
session **without writing a full StreamBase subclass**.

## Polling — `polling_serial.py`

Use `PollingSensorStream` when you have a `read()` function the
framework can call on a fixed schedule. SyncField owns the capture
thread and timestamps each sample immediately after `read()` returns.

Run it:

```bash
python examples/generic_sensor_demo/polling_serial.py
```

## Push — `push_async.py`

Use `PushSensorStream` when your data source is callback-driven
(BLE notify, MQTT, OSC, asyncio task, etc.). You own the producer
thread and call `stream.push(channels)` whenever a new sample arrives.

Run it:

```bash
python examples/generic_sensor_demo/push_async.py
```

Both examples use a fake in-memory source so they run anywhere
without hardware.
```

- [ ] **Step 3: Write the polling example**

```python
# examples/generic_sensor_demo/polling_serial.py
"""PollingSensorStream demo — a fake serial sensor at 50 Hz.

Replace `FakeSerial` with your real `serial.Serial(...)` and you're done.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import syncfield as sf
from syncfield.adapters import PollingSensorStream


class FakeSerial:
    """Stand-in for a real serial.Serial object — emits a sine wave."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def read_sample(self) -> dict[str, float]:
        t = time.monotonic() - self._t0
        return {
            "ax": math.sin(t * 2 * math.pi),
            "ay": math.cos(t * 2 * math.pi),
            "az": 0.5 * math.sin(t * 4 * math.pi),
        }

    def close(self) -> None:
        pass


def main() -> None:
    output_dir = Path("./demo_session_polling")
    output_dir.mkdir(exist_ok=True)

    session = sf.SessionOrchestrator(host_id="demo", output_dir=output_dir)

    serial = FakeSerial()
    session.add(PollingSensorStream(
        "fake_imu",
        read=serial.read_sample,
        hz=50,
        output_dir=output_dir,
    ))

    session.connect()
    session.start()
    print("Recording for 2 seconds...")
    time.sleep(2.0)
    report = session.stop()
    session.disconnect()

    serial.close()

    for f in report.finalizations:
        print(f"  {f.stream_id}: {f.frame_count} samples → {f.file_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Smoke-test it**

Run: `python examples/generic_sensor_demo/polling_serial.py`
Expected: prints `~100 samples → ./demo_session_polling/fake_imu.jsonl`. Verify the file has ~100 lines.

Then clean up: `rm -rf demo_session_polling`

- [ ] **Step 5: Commit**

```bash
git add examples/generic_sensor_demo/README.md examples/generic_sensor_demo/polling_serial.py
git commit -m "examples: PollingSensorStream demo with fake serial sensor"
```

---

### Task 19: Push helper example (asyncio task)

**Files:**
- Create: `examples/generic_sensor_demo/push_async.py`

- [ ] **Step 1: Write the example**

```python
# examples/generic_sensor_demo/push_async.py
"""PushSensorStream demo — an asyncio task pushes samples at 100 Hz.

Replace the fake task with your real BLE/MQTT/OSC/socket loop and you're
done. The key idea: SyncField does not own the producer thread — your
async code does. SyncField just provides a thread-safe `push()` sink.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from pathlib import Path

import syncfield as sf
from syncfield.adapters import PushSensorStream


def main() -> None:
    output_dir = Path("./demo_session_push")
    output_dir.mkdir(exist_ok=True)

    session = sf.SessionOrchestrator(host_id="demo", output_dir=output_dir)

    # The push sink and a holder for the asyncio loop
    loop_holder: dict = {}
    stop_event = asyncio.Event()

    async def fake_ble_loop(stream: PushSensorStream) -> None:
        """Pretend BLE notify task — pushes a sample every 10 ms."""
        t0 = time.monotonic()
        while not stop_event.is_set():
            t = time.monotonic() - t0
            stream.push({
                "ax": math.sin(t * 2 * math.pi),
                "ay": math.cos(t * 2 * math.pi),
            })
            await asyncio.sleep(0.01)

    def on_connect(stream: PushSensorStream) -> None:
        """Spawn the asyncio loop on its own thread."""
        def run():
            loop = asyncio.new_event_loop()
            loop_holder["loop"] = loop
            asyncio.set_event_loop(loop)
            loop.run_until_complete(fake_ble_loop(stream))
            loop.close()

        thread = threading.Thread(target=run, daemon=True)
        loop_holder["thread"] = thread
        thread.start()

    def on_disconnect(stream: PushSensorStream) -> None:
        loop = loop_holder.get("loop")
        if loop is not None:
            loop.call_soon_threadsafe(stop_event.set)
        thread = loop_holder.get("thread")
        if thread is not None:
            thread.join(timeout=1.0)

    push_stream = PushSensorStream(
        "fake_ble_imu",
        output_dir=output_dir,
        on_connect=on_connect,
        on_disconnect=on_disconnect,
    )
    session.add(push_stream)

    session.connect()
    session.start()
    print("Recording for 2 seconds...")
    time.sleep(2.0)
    report = session.stop()
    session.disconnect()

    for f in report.finalizations:
        print(f"  {f.stream_id}: {f.frame_count} samples → {f.file_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test it**

Run: `python examples/generic_sensor_demo/push_async.py`
Expected: prints `~200 samples → ./demo_session_push/fake_ble_imu.jsonl`. Verify the file is well-formed JSONL.

Then clean up: `rm -rf demo_session_push`

- [ ] **Step 3: Commit**

```bash
git add examples/generic_sensor_demo/push_async.py
git commit -m "examples: PushSensorStream demo with asyncio producer"
```

---

## Final Verification

### Task 20: Run the entire test suite once

- [ ] **Step 1: Run all unit + integration tests**

Run: `pytest tests/ -v`
Expected: all tests pass. Note any pre-existing failures (unrelated to this change) and confirm they were failing on `main` before this work too.

- [ ] **Step 2: Run only the new helpers' tests for confidence**

Run: `pytest tests/unit/adapters/test_generic_internals.py tests/unit/adapters/test_polling_sensor.py tests/unit/adapters/test_push_sensor.py tests/integration/adapters/ tests/integration/test_generic_sensor_e2e.py -v`
Expected: ~50 tests, all green.

- [ ] **Step 3: Confirm the public API is clean**

Run a quick interactive sanity check:

```bash
python -c "
import syncfield as sf
from syncfield.adapters import PollingSensorStream, PushSensorStream, JSONLFileStream
print('OK')
print('polling kind:', PollingSensorStream('x', read=lambda: {'a':1}, hz=10, output_dir='/tmp').kind)
print('push kind:', PushSensorStream('x', output_dir='/tmp').kind)
"
```

Expected: prints `OK`, `polling kind: sensor`, `push kind: sensor`.

- [ ] **Step 4: No commit needed — verification only**

---

## Self-Review

**Spec coverage check:**

| Spec section | Tasks |
|---|---|
| `_SensorWriteCore` (frame counter, write, lock, first/last_at) | 1, 2 |
| `_default_sensor_capabilities` / `_resolve_capabilities` | 3 |
| `PollingSensorStream` skeleton + arity detection | 4 |
| `PollingSensorStream._capture_once` happy path | 5 |
| `PollingSensorStream` disk write while recording | 6 |
| `PollingSensorStream` error handling (drop/stop) | 7 |
| `PollingSensorStream` 4-phase lifecycle | 8 |
| `PollingSensorStream` re-export | 9 |
| `PushSensorStream` skeleton | 10 |
| `PushSensorStream` 4-phase lifecycle | 11 |
| `PushSensorStream.push()` happy path | 12 |
| `PushSensorStream.push()` error handling | 13 |
| `PushSensorStream` re-export | 14 |
| Threading integration test (polling) | 15 |
| Threading stress test (push) | 16 |
| End-to-end with orchestrator | 17 |
| Examples — polling + push | 18, 19 |
| Final test sweep | 20 |

Every section of the spec has at least one task. No gaps.

**Placeholder scan:** None — every code step contains the actual code, every test step contains the actual assertions.

**Type consistency:** `_SensorWriteCore`, `_resolve_capabilities`, `PollingSensorStream`, `PushSensorStream`, `_capture_once`, `push()` — names are stable across all tasks. The shared internals module is consistently `_generic.py`.

**Open risk:** Task 17 (e2e) depends on the actual `SessionOrchestrator` constructor signature. The plan instructs the implementer to confirm it before writing the test. This is the only spot where the plan defers to the implementer rather than prescribing exact code, and it does so deliberately because the orchestrator is outside the scope of this work.
