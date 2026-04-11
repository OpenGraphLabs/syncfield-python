# Generic Sensor Stream Helpers — Design Spec

**Date:** 2026-04-09
**Status:** Draft — pending user review
**Owner:** styu12

## Problem

SyncField currently exposes capture sources only through full
`StreamBase` subclasses. Every new device — even a trivial serial sensor
or BLE notify-only peripheral — requires a dedicated adapter file with
its own thread/asyncio plumbing, lifecycle implementation, capabilities
declaration, and tests. The reference adapters
(`UVCWebcamStream`, `BLEImuGenericStream`, `OgloTactileStream`,
`OakCameraStream`) prove this works, but they also expose the cost: each
new sensor that doesn't fit one of the existing adapters faces the same
~200-line ceremony.

There is currently **no first-class path** for a user to attach a simple
sensor without writing a full adapter:

- `JSONLFileStream` (`adapters/jsonl_file.py:21`) is the only escape
  hatch, but it forces the user to write the JSONL file themselves —
  SyncField just tracks lifecycle and counts lines on stop.
- `BLEImuGenericStream` is "generic" only across BLE notify devices;
  it gives no leverage for serial, I2C, OSC, MQTT, or any custom
  callback-driven source.
- `FakeStream.push_sample()` (`testing.py:98`) demonstrates the pattern
  we want, but is explicitly marked test-only.

A second observation drove the scope of this design: **`SensorWriter`
(`writer.py:73`) exists with full unit tests but is not used by any
adapter today.** Sensor adapters all advertise `produces_file=False` and
emit only in-memory `SampleEvent`s. This means the generic helpers will
be the first adapters to actually persist sensor JSONL to disk through
the SDK pipeline — establishing the convention that future BLE/Oglo
extensions can adopt.

## Goals

1. Let a user attach **most simple sensors** to a `SessionOrchestrator`
   in roughly **one statement**, without subclassing `StreamBase`.
2. Cover both natural device shapes:
   - **Polling** — user has a `read()` function the framework can call
     in a loop (serial, I2C, SPI, REST polling).
   - **Push** — user has callback/async/external thread sources
     (BLE notify, OSC, MQTT, ZeroMQ, asyncio tasks).
3. Treat helper-backed streams as **first-class citizens**: full
   4-phase lifecycle, live preview during `CONNECTED`, viewer parity,
   `device_key` dedup, capabilities declaration.
4. Establish the **first reference implementation** of sensor JSONL
   persistence through `SensorWriter`, so future "real" sensor adapters
   (BLE IMU, Oglo) can adopt the same pattern when they need disk
   output.
5. Stay **YAGNI** in v1: sensors only. Cameras follow in v2 once we
   have learned from the v1 surface.

## Non-Goals

- **Cameras / frame streams.** Deferred to v2. Cameras need codec,
  container, frame buffer, and `latest_frame` plumbing that is
  fundamentally different from sample streams. Folding both into one
  helper would muddle the v1 design.
- **Discovery integration.** Generic helpers wrap user-supplied code;
  there is nothing to enumerate. `register_discoverer` is not called.
- **Replacing existing adapters.** UVC/BLE/Oglo/OAK stay as-is. The
  helpers are a parallel path, not a migration target.
- **Async-native API.** The helpers stay synchronous to match the
  current `Stream` SPI. Users with asyncio sources bridge into the push
  helper from their own loop, exactly the way `OgloTactileStream` does
  internally today.
- **Thread pool / batching.** Each polling stream owns one thread.
  No shared executor.

## Design Decisions (locked during brainstorming)

1. **Tiered architecture (c).** Helpers are thin first-class entry
   points. Internally each helper IS a `StreamBase` subclass — there is
   no separate "compile to adapter" step. Users who outgrow a helper
   graduate to writing their own `StreamBase` subclass; the mental
   model is identical.
2. **v1 = sensors only.** Cameras are a separate v2 design.
3. **Hybrid: two helpers.** `PollingSensorStream` and
   `PushSensorStream` are explicit, named separately. One unified
   helper with conditional behavior was rejected because the timing
   contracts differ (polling captures `capture_ns` itself; push
   delegates to the user).
4. **Live preview supported.** Both helpers implement the full 4-phase
   lifecycle (`prepare → connect → start_recording → stop_recording →
   disconnect`). Helper-backed streams render in the viewer during
   `CONNECTED`, just like UVC.

## Architecture

```
src/syncfield/adapters/
├── _generic.py          ← NEW: shared internals (writer wiring,
│                              frame counter, thread-safe sample core)
├── polling_sensor.py    ← NEW: PollingSensorStream
├── push_sensor.py       ← NEW: PushSensorStream
├── jsonl_file.py        (unchanged)
├── uvc_webcam.py        (unchanged)
├── ble_imu.py           (unchanged)
├── oglo_tactile.py      (unchanged)
├── oak_camera.py        (unchanged)
└── __init__.py          (re-export the two new helpers)
```

`_generic.py` holds:

- **`_SensorWriteCore`** — owns the `SensorWriter`, the frame counter,
  the `first_sample_at_ns` / `last_sample_at_ns` trackers, and the
  thread-safe `write()` entry point. Both helpers compose one of these
  rather than inheriting it. Single source of truth for "how a sensor
  helper persists samples."
- **`_default_sensor_capabilities(precise: bool) -> StreamCapabilities`**
  — sensible defaults: `provides_audio_track=False`,
  `is_removable=False`, `produces_file=True`,
  `supports_precise_timestamps=precise`. Polling defaults
  `precise=True` (helper captures `capture_ns` immediately after
  `read()`); push defaults `precise=False` (the user may or may not
  pass a hardware timestamp).
- **`_resolve_capabilities(user_caps, *, precise)`** — if the user
  passed a `capabilities=` kwarg, use it; otherwise return the
  default.

This module is private (`_generic`). Nothing in it is exported.

### `PollingSensorStream`

```python
from typing import Any, Callable, Literal
from pathlib import Path
from syncfield.types import ChannelValue, StreamCapabilities
from syncfield.stream import StreamBase, DeviceKey

class PollingSensorStream(StreamBase):
    def __init__(
        self,
        id: str,
        *,
        read: Callable[..., dict[str, ChannelValue]],
        hz: float,
        output_dir: Path | str,
        open: Callable[[], Any] | None = None,
        close: Callable[[Any], None] | None = None,
        device_key: DeviceKey | None = None,
        capabilities: StreamCapabilities | None = None,
        on_read_error: Literal["drop", "stop"] = "drop",
    ) -> None: ...
```

**Lifecycle mapping:**

| Phase | Behavior |
|---|---|
| `__init__` | Store callbacks, instantiate `_SensorWriteCore`, resolve capabilities. **No I/O.** |
| `prepare()` | No-op. |
| `connect()` | `self._handle = self._open() if self._open else None`; clear `_stop_event`; spawn capture thread. Capture thread enters the read loop and emits samples via `_emit_sample()`. **No disk write yet.** |
| `start_recording(clock)` | `_write_core.open()`; flip `_writing = True`. The (already running) capture thread starts persisting samples on its next iteration. |
| `stop_recording()` | Flip `_writing = False`; `_write_core.close()`; return a `FinalizationReport`. **Capture thread keeps running** so preview continues during `CONNECTED`. |
| `disconnect()` | `_stop_event.set()`; `thread.join(timeout=3.0)`; `self._close(self._handle)` if provided; `self._handle = None`. |

**Capture loop:**

```python
def _capture_once(self) -> bool:
    """One iteration of the capture loop. Returns False to halt."""
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

    capture_ns = time.monotonic_ns()  # immediately after read
    frame_number = self._write_core.next_frame_number()
    self._emit_sample(SampleEvent(self.id, frame_number, capture_ns, channels))
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
                time.monotonic_ns(), f"sensor write failed: {exc}",
            ))
    elapsed = time.monotonic() - loop_start
    time.sleep(max(0, self._period - elapsed))
    return True

def _capture_loop(self) -> None:
    while not self._stop_event.is_set():
        if not self._capture_once():
            return
```

The split between `_capture_loop` and `_capture_once` is deliberate —
unit tests drive `_capture_once()` directly without spawning a thread.
This is the only test-friendliness concession in the production code
and it costs nothing in clarity.

**`read` arity detection:** the helper inspects `read` with
`inspect.signature()` once in `__init__`. If `open` was provided, the
helper expects `read(handle)`; otherwise `read()`. A clear `TypeError`
is raised at construction if the arity does not match.

### `PushSensorStream`

```python
class PushSensorStream(StreamBase):
    def __init__(
        self,
        id: str,
        *,
        output_dir: Path | str,
        on_connect: Callable[["PushSensorStream"], None] | None = None,
        on_disconnect: Callable[["PushSensorStream"], None] | None = None,
        device_key: DeviceKey | None = None,
        capabilities: StreamCapabilities | None = None,
    ) -> None: ...

    def push(
        self,
        channels: dict[str, ChannelValue],
        *,
        capture_ns: int | None = None,
        frame_number: int | None = None,
    ) -> None: ...
```

**Lifecycle mapping:**

| Phase | Behavior |
|---|---|
| `__init__` | Store callbacks, instantiate `_SensorWriteCore`, resolve capabilities. |
| `prepare()` | No-op. |
| `connect()` | `self._connected = True`; `self._on_connect(self)` if provided. After this call, `push()` is permitted. |
| `start_recording(clock)` | `_write_core.open()`; `self._writing = True`. |
| `stop_recording()` | `self._writing = False`; `_write_core.close()`; return `FinalizationReport`. **User thread is not touched** — `push()` keeps being accepted, samples just aren't persisted. |
| `disconnect()` | `self._connected = False`; `self._on_disconnect(self)` if provided. Subsequent `push()` calls are dropped with a `WARNING`. |

**`push()` semantics:**

```python
def push(self, channels, *, capture_ns=None, frame_number=None):
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
    with self._push_lock:
        if frame_number is None:
            frame_number = self._write_core.next_frame_number()
        self._emit_sample(SampleEvent(
            self.id, frame_number, capture_ns, channels,
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

Key invariants:

- **`push()` never raises** for SyncField-internal failures. The user
  thread (often a BLE callback or asyncio task) must not be killed by
  the SDK. Bad channel types raise `TypeError` immediately because
  silently dropping a user-code bug is worse.
- **`_push_lock` covers the entire push body.** Frame number
  assignment, callback emission, and write are all serialized so
  multi-producer push sites observe consistent ordering. Subscribers
  must be cheap and non-blocking — this is documented on the helper.
- **`capture_ns` defaults to `time.monotonic_ns()` at push time.**
  Users with hardware timestamps pass them explicitly.

## Data Flow

```
┌──────────────┐    user-supplied    ┌─────────────────────┐
│ user code    │ ──────────────────▶ │ PollingSensorStream │
│ (read fn)    │      read()         │ (capture thread)    │
└──────────────┘                     └──────────┬──────────┘
                                                │ _emit_sample
                                                │ (always)
                                                ▼
                                     ┌─────────────────────┐
                                     │ on_sample callbacks │
                                     │ (orchestrator,      │
                                     │  viewer poller)     │
                                     └─────────────────────┘
                                                │
                                                │ if _writing
                                                ▼
                                     ┌─────────────────────┐
                                     │ _SensorWriteCore    │
                                     │   → SensorWriter    │
                                     │   → {id}.jsonl      │
                                     └─────────────────────┘
```

For `PushSensorStream` the same diagram holds but the leftmost arrow
runs in the *user's* thread (BLE callback, asyncio task, custom loop)
rather than a helper-owned capture thread. The lock at the entrance to
`push()` is what makes the rest of the diagram identical.

## Error Handling

| Situation | Behavior |
|---|---|
| `read()` raises (polling) | `HealthEvent(ERROR)`, sample dropped, loop continues. `on_read_error="stop"` ends the capture loop. |
| `read()` returns non-dict | `HealthEvent(ERROR)`, sample dropped, loop continues. `on_read_error="stop"` ends the loop. |
| `open()` raises (polling) | Bubbles up out of `connect()` so the orchestrator runs its rollback path. |
| `close()` raises (polling) | `HealthEvent(WARNING)`, swallowed. `disconnect()` must always succeed. |
| `push()` called before `connect()` | `HealthEvent(WARNING)`, sample dropped. **Never raises.** |
| `push()` called after `disconnect()` | Same. |
| `push(channels=42)` | `TypeError` raised immediately. User-code bug; failing loud is safer than silent drop. |
| Sensor JSONL write fails | `HealthEvent(ERROR)`, sample dropped, next sample retries. (Auto-stop after N consecutive failures is deferred to v1.1.) |
| Capture thread join timeout (polling, disconnect) | `HealthEvent(WARNING)`. The thread is daemon, so process exit cleans it up. |
| `disconnect()` called between record sessions while `_writing=True` | Cannot happen — orchestrator guarantees `stop_recording()` runs first. |

## Testing Strategy

Four layers, mostly independent.

### Layer 1 — Unit (no threading)

`tests/unit/adapters/test_polling_sensor.py`,
`tests/unit/adapters/test_push_sensor.py`

Drive the protected `_capture_once()` hook directly. Push helper
already supports thread-free testing because `connect()`/`disconnect()`
do not spawn anything (the user thread is the user's responsibility).

Scenarios per helper:

- Happy path `prepare → connect → start_recording → samples → stop_recording → disconnect`: JSONL line count, `FinalizationReport` correctness.
- Multi-record cycle: connect → record → stop → record → stop → disconnect. Verify only the recording windows hit disk.
- `read()` raises with `on_read_error="drop"` — `HealthEvent(ERROR)`, sample dropped, next iteration succeeds.
- `read()` raises with `on_read_error="stop"` — `_capture_once()` returns `False`.
- `read()` returns wrong type — same drop/stop split.
- `push()` before `connect()` / after `disconnect()` — `HealthEvent(WARNING)`, no raise.
- `push(channels=42)` — `TypeError`.
- `open()` raises — bubbles out of `connect()`.
- `close()` raises — `disconnect()` succeeds with `HealthEvent(WARNING)`.
- Frame counter monotonic across preview→record→preview→record cycle.
- Capabilities defaults vs user override.
- `device_key` round-trip.
- `_SensorWriteCore` `first_sample_at_ns` / `last_sample_at_ns` tracking.

### Layer 2 — Threading integration

`tests/integration/adapters/test_polling_sensor_threading.py`,
`tests/integration/adapters/test_push_sensor_threading.py`

- Polling: deterministic fake `read()` returning a counter dict.
  Run `hz=1000` for ~100ms and verify sample count is within ±20%.
- Push: N producer threads × M pushes each. Verify exact JSONL line
  count, monotonic frame numbers, no lost samples, no torn lines.

These tests have a flaky risk by nature. If they prove unreliable on
CI we will switch to a deterministic clock injection through
`SessionClock` rather than wallclock sleeps.

### Layer 3 — End-to-end with orchestrator

`tests/integration/test_generic_sensor_e2e.py`

Real `SessionOrchestrator` + one `PollingSensorStream` + one
`PushSensorStream`. Walk one full session cycle. Verify
`manifest.json`, `sync_point.json`, both `{id}.jsonl` files,
`FinalizationReport.frame_count` matches the JSONL line count, and
both helpers appear in `manifest["streams"]` with the right
capabilities round-trip.

This is the test that proves helper-backed streams are first-class
citizens of the SyncField pipeline.

### Layer 4 — Manual smoke (examples)

`examples/generic_sensor_demo/`:

- `polling_serial.py` — five-line example using a fake serial source
  (so the example runs anywhere, no hardware required).
- `push_async.py` — five-line asyncio example using `PushSensorStream`.

These are not automated; they live in the README and exist to prove
the "one statement" promise to a human reader.

## Capabilities Defaults

```python
# Polling default
StreamCapabilities(
    provides_audio_track=False,
    supports_precise_timestamps=True,   # we time-stamp right after read()
    is_removable=False,
    produces_file=True,
)

# Push default
StreamCapabilities(
    provides_audio_track=False,
    supports_precise_timestamps=False,  # depends on whether user passes capture_ns
    is_removable=False,
    produces_file=True,
)
```

Users with removable hardware (USB, BLE) override `is_removable=True`
via the `capabilities=` kwarg.

## Public API Surface

Two new symbols re-exported from `syncfield.adapters` and (by
extension) from `syncfield`:

```python
# syncfield/adapters/__init__.py
from syncfield.adapters.polling_sensor import PollingSensorStream
from syncfield.adapters.push_sensor import PushSensorStream

__all__ = [
    "JSONLFileStream",
    "PollingSensorStream",  # NEW
    "PushSensorStream",     # NEW
    # ... optional re-exports as today
]
```

These do not require any optional extra — they only depend on the
standard library and the existing SyncField core.

## Forward Compatibility (v2 cameras)

The v2 camera helper (`PollingFrameStream`, `PushFrameStream`) will
follow the same architectural pattern: thin first-class helper,
shared `_generic.py`-style internals (a `_FrameWriteCore` paralleling
`_SensorWriteCore`), full 4-phase lifecycle. The naming in v1 leaves
room for the parallel: `PollingSensorStream` ↔ `PollingFrameStream`,
`PushSensorStream` ↔ `PushFrameStream`. Nothing in v1 forecloses on
v2 — but v1 also does not pre-build any v2 abstractions (YAGNI).

## Open Questions Deferred to Implementation

- **Auto-stop after N consecutive write failures.** Listed under
  Error Handling as v1.1. v1 lets writes keep failing, which is
  acceptable because each failure produces a `HealthEvent(ERROR)` that
  the operator will see in the session log.
- **`output_dir` injection from the orchestrator.** Today every
  file-producing adapter takes `output_dir` in its constructor (UVC,
  the proposed helpers). Cleaner would be for the orchestrator to
  inject it at `add()` time. That's a separate refactor that touches
  UVC and OAK too — out of scope for this spec.

## Summary

Two new helper classes —  `PollingSensorStream` and
`PushSensorStream` — wrapped around a small private `_generic.py`
module that owns sensor JSONL persistence. Both are full
`StreamBase` subclasses with 4-phase lifecycle, live preview, and
device-key dedup. Sensors only in v1; cameras follow in v2 using the
same architectural pattern. The helpers are also the first SDK
adapters to actually use `SensorWriter` for disk persistence,
establishing the convention for future BLE/Oglo extensions.
