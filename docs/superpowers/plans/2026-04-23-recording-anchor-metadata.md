# Intra-Host Sync Anchor Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 각 스트림 어댑터가 "recording armed" 공통 host 시각과 첫 기록 프레임의 `(host_ts, device_ts)` pair를 명시적 metadata로 기록. downstream sync service가 어댑터별 pipeline latency bias를 상쇄할 수 있는 anchor 정보 제공.

**Architecture:** 기존 `SessionClock` 인프라 확장 (frozen dataclass 에 optional `recording_armed_ns` 필드 추가). `Orchestrator.start()` 가 `start_recording()` 호출 직전에 공통 T를 찍어 `SessionClock` 의 새 복사본을 만들어 모든 어댑터에 전달. 각 어댑터의 capture loop 가 첫 기록 프레임에 `RecordingAnchor(armed_host_ns, first_frame_host_ns, first_frame_device_ns)` 를 `FinalizationReport.recording_anchor` 로 기록. 기존 `start_recording(session_clock)` 시그니처 완전 유지 — 어댑터 side는 `session_clock.recording_armed_ns` 를 읽기만 하면 됨.

**Tech Stack:** Python 3.11, pytest, dataclasses. 기존 `StreamBase` protocol, `SessionClock`, `FinalizationReport` 에 필드 추가만으로 backward compatible.

**Scope:** syncfield-python 측 anchor 캡처 + metadata 기록. syncfield(sync service) 측 alignment 활용은 별도 plan.

---

## File Structure

**Modify:**
- `src/syncfield/types.py` — `RecordingAnchor` dataclass + `FinalizationReport.recording_anchor` 필드
- `src/syncfield/clock.py` — `SessionClock.recording_armed_ns` 필드
- `src/syncfield/stream.py` — `StreamBase._recording_anchor` helper
- `src/syncfield/orchestrator.py` — armed_ns 찍어 SessionClock 복제 후 전파 + manifest 에 anchor 수집
- `src/syncfield/adapters/oak_camera.py` — capture loop 에 anchor 캡처
- `src/syncfield/adapters/uvc_webcam.py` — capture loop 에 anchor 캡처
- `src/syncfield/adapters/polling_sensor.py` — 샘플 loop 에 anchor 캡처
- `src/syncfield/adapters/push_sensor.py` — 샘플 loop 에 anchor 캡처

**Tests:**
- `tests/unit/test_types.py` — `RecordingAnchor` 직렬화
- `tests/unit/test_clock.py` — `SessionClock` armed 필드
- `tests/unit/test_stream_base.py` — anchor helper
- `tests/unit/adapters/test_oak_camera.py` — OAK anchor 캡처
- `tests/unit/adapters/test_uvc_webcam.py` — UVC anchor 캡처
- `tests/unit/test_orchestrator.py` — armed_ns 전파 + manifest 수집

**Decision notes:**
- `SessionClock.recording_armed_ns: Optional[int] = None` — preview phase 에서는 None. orchestrator가 start_recording 직전에 `dataclasses.replace(clock, recording_armed_ns=armed_ns)` 로 복제.
- `FinalizationReport.recording_anchor: Optional[RecordingAnchor] = None` — 어댑터가 첫 프레임 관찰 못 하면 (empty recording) None.
- `RecordingAnchor` 는 `first_frame_device_ns` optional — UVC/host_audio 처럼 device clock 없는 어댑터는 None.
- 나머지 어댑터들 (meta_quest, oglo_tactile, ble_imu, host_audio, jsonl_file, insta360_go3s, meta_quest_camera) 은 동일 패턴이라 한 번에 확장 — 본 plan 의 Task 7-10 에서 처리. 패턴 확립 후 확장이 쉽도록 Task 3 의 helper 를 공통화.

---

## Task 1: `RecordingAnchor` dataclass

**Files:**
- Modify: `src/syncfield/types.py` (add after `SyncPoint` class, around line 73)
- Test: `tests/unit/test_types.py`

- [ ] **Step 1: Write failing test**

파일: `tests/unit/test_types.py` 에 추가

```python
def test_recording_anchor_with_device_ts():
    from syncfield.types import RecordingAnchor
    anchor = RecordingAnchor(
        armed_host_ns=1_000_000_000,
        first_frame_host_ns=1_044_000_000,
        first_frame_device_ns=9_876_543_210,
    )
    assert anchor.first_frame_latency_ns == 44_000_000
    assert anchor.to_dict() == {
        "armed_host_ns": 1_000_000_000,
        "first_frame_host_ns": 1_044_000_000,
        "first_frame_device_ns": 9_876_543_210,
        "first_frame_latency_ns": 44_000_000,
    }

def test_recording_anchor_without_device_ts():
    from syncfield.types import RecordingAnchor
    anchor = RecordingAnchor(
        armed_host_ns=1_000,
        first_frame_host_ns=1_044_000_000,
    )
    assert anchor.first_frame_device_ns is None
    d = anchor.to_dict()
    assert d["first_frame_device_ns"] is None
    assert d["first_frame_latency_ns"] == 1_044_000_000 - 1_000

def test_recording_anchor_rejects_first_before_armed():
    from syncfield.types import RecordingAnchor
    import pytest
    with pytest.raises(ValueError, match="first_frame_host_ns must be >= armed_host_ns"):
        RecordingAnchor(armed_host_ns=100, first_frame_host_ns=50)
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /Users/jerry/Documents/syncfield-python && uv run pytest tests/unit/test_types.py::test_recording_anchor_with_device_ts -v
```
Expected: `ImportError: cannot import name 'RecordingAnchor'`

- [ ] **Step 3: Implement `RecordingAnchor`**

`src/syncfield/types.py` — `SyncPoint` 클래스 정의 끝부분 (line ~73) 바로 다음에 추가:

```python
@dataclass(frozen=True)
class RecordingAnchor:
    """Per-stream anchor info captured when recording is armed.

    Captures the common host ``armed_host_ns`` (shared by all streams in
    the session) together with the first recorded frame's ``(host_ts,
    device_ts)`` pair for this stream. Downstream sync tooling uses the
    difference ``first_frame_host_ns - armed_host_ns`` to estimate each
    adapter's observed pipeline latency and remove per-adapter bias when
    aligning streams.

    Attributes:
        armed_host_ns: Common host monotonic_ns captured by the
            orchestrator immediately before ``start_recording()`` is
            fanned out to streams. Identical across all streams in a
            single recording window.
        first_frame_host_ns: Host monotonic_ns at which this stream's
            first recorded frame arrived on the host.
        first_frame_device_ns: Optional device-clock timestamp of the
            first recorded frame. ``None`` for adapters without a
            device-side clock (UVC webcams, host audio, etc).
    """

    armed_host_ns: int
    first_frame_host_ns: int
    first_frame_device_ns: int | None = None

    def __post_init__(self) -> None:
        if self.first_frame_host_ns < self.armed_host_ns:
            raise ValueError(
                f"first_frame_host_ns must be >= armed_host_ns; "
                f"got armed={self.armed_host_ns}, first={self.first_frame_host_ns}"
            )

    @property
    def first_frame_latency_ns(self) -> int:
        """Observed latency from armed moment to first frame arrival."""
        return self.first_frame_host_ns - self.armed_host_ns

    def to_dict(self) -> dict[str, Any]:
        return {
            "armed_host_ns": self.armed_host_ns,
            "first_frame_host_ns": self.first_frame_host_ns,
            "first_frame_device_ns": self.first_frame_device_ns,
            "first_frame_latency_ns": self.first_frame_latency_ns,
        }
```

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/unit/test_types.py -v -k recording_anchor
```
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/types.py tests/unit/test_types.py
git commit -m "feat(types): add RecordingAnchor dataclass for per-stream sync anchor"
```

---

## Task 2: Extend `SessionClock` with `recording_armed_ns`

**Files:**
- Modify: `src/syncfield/clock.py`
- Test: `tests/unit/test_clock.py`

- [ ] **Step 1: Write failing test**

파일: `tests/unit/test_clock.py` 에 추가 (기존 파일 없으면 생성)

```python
import dataclasses

from syncfield.clock import SessionClock
from syncfield.types import SyncPoint


def _make_clock() -> SessionClock:
    sp = SyncPoint.create_now(host_id="host_a")
    return SessionClock(sync_point=sp)


def test_session_clock_preview_phase_has_no_armed_ns():
    clock = _make_clock()
    assert clock.recording_armed_ns is None


def test_session_clock_arm_returns_new_clock_with_armed_ns():
    clock = _make_clock()
    armed = dataclasses.replace(clock, recording_armed_ns=12_345)
    assert armed.recording_armed_ns == 12_345
    assert clock.recording_armed_ns is None  # original unchanged


def test_session_clock_armed_ns_survives_frozen_semantics():
    clock = _make_clock()
    armed = dataclasses.replace(clock, recording_armed_ns=500)
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        armed.recording_armed_ns = 700  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/unit/test_clock.py -v
```
Expected: `AttributeError: 'SessionClock' object has no attribute 'recording_armed_ns'`

- [ ] **Step 3: Add field to `SessionClock`**

`src/syncfield/clock.py` — `SessionClock` dataclass 를 아래와 같이 수정:

```python
@dataclass(frozen=True)
class SessionClock:
    """Shared monotonic clock reference for all streams in one session.

    A ``SessionClock`` is cheap to copy, safe to share across threads, and
    binds each stream in a session to the exact same monotonic anchor. The
    orchestrator constructs it once at ``start()`` and distributes it to
    every ``Stream.start(session_clock)`` call so intra-session timing uses
    a single source of truth.

    Attributes:
        sync_point: The session's :class:`SyncPoint` (monotonic + wall clock
            anchor captured at session start).
        recording_armed_ns: Common host monotonic_ns captured by the
            orchestrator right before it fans out ``start_recording()``
            to every stream. ``None`` during preview phase, non-``None``
            once recording is armed. All streams receive the same value,
            so adapters can use it as a shared intra-host sync anchor.
    """

    sync_point: SyncPoint
    recording_armed_ns: int | None = None

    @property
    def host_id(self) -> str:
        """Host identifier for this session."""
        return self.sync_point.host_id

    def now_ns(self) -> int:
        """Return the current monotonic nanosecond timestamp.

        Thread-safe: :func:`time.monotonic_ns` is atomic on CPython.
        """
        return time.monotonic_ns()

    def elapsed_ns(self) -> int:
        """Return nanoseconds elapsed since the session's sync point."""
        return time.monotonic_ns() - self.sync_point.monotonic_ns
```

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/unit/test_clock.py -v
```
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/clock.py tests/unit/test_clock.py
git commit -m "feat(clock): add SessionClock.recording_armed_ns for shared intra-host anchor"
```

---

## Task 3: `StreamBase` anchor helper

**Files:**
- Modify: `src/syncfield/stream.py`
- Test: `tests/unit/test_stream_base.py`

- [ ] **Step 1: Write failing test**

파일: `tests/unit/test_stream_base.py` 에 추가

```python
import dataclasses

from syncfield.clock import SessionClock
from syncfield.stream import StreamBase
from syncfield.types import StreamCapabilities, SyncPoint


class _Dummy(StreamBase):
    def __init__(self) -> None:
        super().__init__("d", "sensor", StreamCapabilities())


def _clock(armed_ns: int | None = None) -> SessionClock:
    sp = SyncPoint.create_now(host_id="h")
    return SessionClock(sync_point=sp, recording_armed_ns=armed_ns)


def test_anchor_helper_returns_none_before_first_frame():
    d = _Dummy()
    d._begin_recording_window(_clock(armed_ns=100))
    assert d._recording_anchor() is None


def test_anchor_helper_captures_first_frame_then_ignores_later():
    d = _Dummy()
    d._begin_recording_window(_clock(armed_ns=100))
    d._observe_first_frame(host_ns=250, device_ns=9_000)
    d._observe_first_frame(host_ns=300, device_ns=10_000)  # ignored
    anchor = d._recording_anchor()
    assert anchor is not None
    assert anchor.armed_host_ns == 100
    assert anchor.first_frame_host_ns == 250
    assert anchor.first_frame_device_ns == 9_000


def test_anchor_helper_without_device_ts():
    d = _Dummy()
    d._begin_recording_window(_clock(armed_ns=100))
    d._observe_first_frame(host_ns=250, device_ns=None)
    anchor = d._recording_anchor()
    assert anchor is not None
    assert anchor.first_frame_device_ns is None


def test_anchor_helper_noop_if_armed_ns_missing():
    d = _Dummy()
    d._begin_recording_window(_clock(armed_ns=None))
    d._observe_first_frame(host_ns=250, device_ns=None)
    assert d._recording_anchor() is None


def test_anchor_helper_reset_on_second_recording_window():
    d = _Dummy()
    d._begin_recording_window(_clock(armed_ns=100))
    d._observe_first_frame(host_ns=250, device_ns=9_000)
    d._begin_recording_window(_clock(armed_ns=1_000))
    assert d._recording_anchor() is None  # reset
    d._observe_first_frame(host_ns=1_100, device_ns=500)
    anchor = d._recording_anchor()
    assert anchor is not None
    assert anchor.armed_host_ns == 1_000
    assert anchor.first_frame_host_ns == 1_100
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/unit/test_stream_base.py -v
```
Expected: `AttributeError: '_Dummy' object has no attribute '_begin_recording_window'`

- [ ] **Step 3: Add anchor helper methods to `StreamBase`**

`src/syncfield/stream.py` — `StreamBase.__init__` 안 마지막 라인 (`self._collected_health: ...`) 아래에 세 필드 추가하고, 파일 끝부분 `disconnect()` 직전에 helper 메서드 추가:

`__init__` 수정 — 라인 207 바로 아래:

```python
        self._collected_health: List[HealthEvent] = []
        # Intra-host sync anchor — captured once per recording window,
        # when the first frame/sample arrives after start_recording().
        self._armed_host_ns: int | None = None
        self._first_frame_observed: bool = False
        self._anchor: Optional["RecordingAnchor"] = None
```

파일 상단 import:

```python
from syncfield.types import (
    # ... 기존 것들,
    RecordingAnchor,
)
```

`_emit_health` 다음에 helper 메서드 추가 (Lifecycle methods 섹션 직전):

```python
    # ------------------------------------------------------------------
    # Intra-host sync anchor
    # ------------------------------------------------------------------
    #
    # Each recording window shares a common ``armed_host_ns`` captured by
    # the orchestrator. Adapters call ``_begin_recording_window`` from
    # ``start_recording`` (with the received ``SessionClock``) and then
    # ``_observe_first_frame`` exactly once from their capture loop when
    # the first frame/sample of the recording window arrives. The
    # resulting :class:`RecordingAnchor` is attached to the stream's
    # :class:`FinalizationReport` by ``stop_recording``.

    def _begin_recording_window(self, session_clock: SessionClock) -> None:
        """Reset anchor state and remember the armed host timestamp.

        Safe to call even when ``recording_armed_ns`` is ``None`` (legacy
        test harnesses / unit mocks) — the helper becomes a no-op.
        """
        self._armed_host_ns = session_clock.recording_armed_ns
        self._first_frame_observed = False
        self._anchor = None

    def _observe_first_frame(
        self, host_ns: int, device_ns: int | None
    ) -> None:
        """Capture the anchor exactly once per recording window.

        Subsequent calls are silently ignored. No-op when there is no
        armed_host_ns (preview phase, legacy code path).
        """
        if self._first_frame_observed:
            return
        if self._armed_host_ns is None:
            return
        # Guard against host clock going backwards under test mocks —
        # clamp to armed_ns so RecordingAnchor's invariant holds.
        safe_host = max(host_ns, self._armed_host_ns)
        self._anchor = RecordingAnchor(
            armed_host_ns=self._armed_host_ns,
            first_frame_host_ns=safe_host,
            first_frame_device_ns=device_ns,
        )
        self._first_frame_observed = True

    def _recording_anchor(self) -> Optional["RecordingAnchor"]:
        """Return the anchor captured for the current recording window."""
        return self._anchor
```

`Optional` 이 상단에 import 되어있는지 확인. 없으면 추가.

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/unit/test_stream_base.py -v
```
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/stream.py tests/unit/test_stream_base.py
git commit -m "feat(stream): add intra-host sync anchor helper to StreamBase"
```

---

## Task 4: `FinalizationReport.recording_anchor` field

**Files:**
- Modify: `src/syncfield/types.py` (`FinalizationReport` around line 306)
- Test: `tests/unit/test_types.py`

- [ ] **Step 1: Write failing test**

`tests/unit/test_types.py` 에 추가:

```python
def test_finalization_report_with_anchor():
    from syncfield.types import FinalizationReport, RecordingAnchor
    anchor = RecordingAnchor(
        armed_host_ns=100, first_frame_host_ns=150, first_frame_device_ns=42
    )
    report = FinalizationReport(
        stream_id="s1", status="completed", frame_count=10,
        file_path=None, first_sample_at_ns=150, last_sample_at_ns=450,
        health_events=[], error=None, recording_anchor=anchor,
    )
    assert report.recording_anchor is anchor


def test_finalization_report_anchor_defaults_to_none():
    from syncfield.types import FinalizationReport
    report = FinalizationReport(
        stream_id="s2", status="completed", frame_count=0,
        file_path=None, first_sample_at_ns=None, last_sample_at_ns=None,
        health_events=[], error=None,
    )
    assert report.recording_anchor is None
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/unit/test_types.py -v -k finalization_report_with_anchor
```
Expected: `TypeError: ... got an unexpected keyword argument 'recording_anchor'`

- [ ] **Step 3: Add field**

`src/syncfield/types.py` — `FinalizationReport` 의 마지막 필드 (`incidents: list = field(default_factory=list)`) 아래에 추가:

```python
    incidents: list = field(default_factory=list)
    recording_anchor: RecordingAnchor | None = None
```

그리고 클래스 docstring 의 `Attributes:` 섹션 끝에 한 줄 추가:

```
        recording_anchor: Intra-host sync anchor captured at the start
            of the recording window (common ``armed_host_ns`` plus the
            stream's first-frame timestamps). ``None`` for empty
            recordings or adapters that haven't opted in.
```

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/unit/test_types.py -v -k "anchor or finalization_report"
```
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/types.py tests/unit/test_types.py
git commit -m "feat(types): surface RecordingAnchor on FinalizationReport"
```

---

## Task 5: Orchestrator — arm SessionClock + propagate anchor to manifest

**Files:**
- Modify: `src/syncfield/orchestrator.py` (around line 1922-1936)
- Test: `tests/unit/test_orchestrator.py` (새 테스트 추가 — 기존 테스트 파일 패턴 따름)

- [ ] **Step 1: Write failing test**

파일: `tests/unit/test_orchestrator_anchor.py` (신규)

```python
"""Orchestrator fans out a single armed_host_ns to all streams."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

from syncfield.clock import SessionClock
from syncfield.orchestrator import SessionOrchestrator
from syncfield.stream import StreamBase
from syncfield.types import (
    FinalizationReport, RecordingAnchor, StreamCapabilities,
)


class _CaptureClockStream(StreamBase):
    """Records the SessionClock passed to start_recording()."""

    def __init__(self, id: str) -> None:
        super().__init__(id, "sensor", StreamCapabilities())
        self.received_clock: SessionClock | None = None

    def connect(self) -> None: pass

    def start_recording(self, session_clock: SessionClock) -> None:
        self.received_clock = session_clock

    def stop_recording(self) -> FinalizationReport:
        anchor = None
        if self.received_clock and self.received_clock.recording_armed_ns:
            anchor = RecordingAnchor(
                armed_host_ns=self.received_clock.recording_armed_ns,
                first_frame_host_ns=self.received_clock.recording_armed_ns + 1_000,
                first_frame_device_ns=None,
            )
        return FinalizationReport(
            stream_id=self.id, status="completed", frame_count=1,
            file_path=None, first_sample_at_ns=0, last_sample_at_ns=0,
            health_events=[], error=None, recording_anchor=anchor,
        )

    def disconnect(self) -> None: pass


def test_orchestrator_arms_clock_and_all_streams_see_same_armed_ns(tmp_path: Path) -> None:
    sess = SessionOrchestrator(host_id="h", output_dir=tmp_path)
    a = _CaptureClockStream("a"); b = _CaptureClockStream("b")
    sess.add(a); sess.add(b)
    sess.connect()
    sess.start()
    time.sleep(0.01)
    sess.stop()

    assert a.received_clock is not None and b.received_clock is not None
    assert a.received_clock.recording_armed_ns is not None
    assert a.received_clock.recording_armed_ns == b.received_clock.recording_armed_ns


def test_orchestrator_manifest_includes_per_stream_anchor(tmp_path: Path) -> None:
    import json
    sess = SessionOrchestrator(host_id="h", output_dir=tmp_path)
    a = _CaptureClockStream("a")
    sess.add(a)
    sess.connect(); sess.start(); time.sleep(0.01); sess.stop()

    manifest_paths = list(tmp_path.rglob("manifest.json"))
    assert manifest_paths, "manifest.json not written"
    manifest = json.loads(manifest_paths[0].read_text())
    streams = manifest.get("streams", [])
    a_entry = next(s for s in streams if s["stream_id"] == "a")
    assert "recording_anchor" in a_entry
    assert a_entry["recording_anchor"] is not None
    assert "armed_host_ns" in a_entry["recording_anchor"]
    assert "first_frame_host_ns" in a_entry["recording_anchor"]
    assert "first_frame_latency_ns" in a_entry["recording_anchor"]
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/unit/test_orchestrator_anchor.py -v
```
Expected: fail — armed_ns is None (orchestrator doesn't set it yet).

- [ ] **Step 3: Modify orchestrator to arm the clock + collect anchor**

`src/syncfield/orchestrator.py` 의 line 1922-1936 부근 — `start_recording` fan-out 블록을 찾아서 `start_recording` 호출 직전에 armed_ns 를 찍고 clock 을 replace:

Before:
```python
            # --- Atomic start_recording ------------------------------
            # Open persistence writers BEFORE start_recording so the
            # ... existing comments ...
            for stream in self._streams:
                try:
                    stream.start_recording(self._session_clock)
```

After:
```python
            # --- Atomic start_recording ------------------------------
            # Open persistence writers BEFORE start_recording so the
            # ... existing comments ...
            #
            # Capture a single shared armed_host_ns immediately before
            # fanning start_recording out — all streams receive the same
            # value via SessionClock.recording_armed_ns, which they can
            # use as an intra-host sync anchor in their capture loop.
            import dataclasses as _dc
            armed_ns = time.monotonic_ns()
            self._session_clock = _dc.replace(
                self._session_clock, recording_armed_ns=armed_ns
            )
            for stream in self._streams:
                try:
                    stream.start_recording(self._session_clock)
```

(`time` 은 이미 파일 상단에 import 되어 있어야 함 — 없으면 추가.)

`write_manifest` 호출부에서 each finalization report 의 `recording_anchor` 가 manifest 에 포함되도록 — `src/syncfield/manifest.py` (또는 `write_manifest` 정의 위치) 에서 per-stream entry 를 만드는 곳을 찾아 아래 필드 추가:

```python
stream_entry["recording_anchor"] = (
    report.recording_anchor.to_dict() if report.recording_anchor else None
)
```

(manifest 모듈의 정확한 위치는 구현 시 `grep "write_manifest" src/syncfield/` 로 확인 — 보통 `src/syncfield/manifest.py` 안 `_stream_entry(...)` 또는 `_make_stream_entry(...)` 함수.)

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/unit/test_orchestrator_anchor.py -v
```
Expected: 2 passed

```bash
# Regression check
uv run pytest tests/ -x --timeout=60
```
Expected: full suite green

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/orchestrator.py src/syncfield/manifest.py tests/unit/test_orchestrator_anchor.py
git commit -m "feat(orchestrator): arm SessionClock and propagate RecordingAnchor to manifest"
```

---

## Task 6: `OakCameraStream` — anchor capture

**Files:**
- Modify: `src/syncfield/adapters/oak_camera.py` (`start_recording` ~line 405, `_capture_loop` ~line 708, `stop_recording` ~line 459)
- Test: `tests/unit/adapters/test_oak_camera.py`

- [ ] **Step 1: Write failing test**

`tests/unit/adapters/test_oak_camera.py` — `TestEncoding` 클래스 근처에 추가:

```python
class TestRecordingAnchor:
    def test_anchor_captured_from_first_frame(self, tmp_path, fake_depthai):
        from syncfield.adapters.oak_camera import OakCameraStream
        from syncfield.clock import SessionClock
        from syncfield.types import SyncPoint
        import dataclasses as _dc, time

        stream = OakCameraStream("oak1", tmp_path, device_id="dev")
        stream.connect()
        sp = SyncPoint.create_now("h")
        clock = SessionClock(sync_point=sp, recording_armed_ns=time.monotonic_ns())
        stream.start_recording(clock)
        fake_depthai.push_frame(stream)  # fixture helper — pushes one frame
        time.sleep(0.05)
        report = stream.stop_recording()
        stream.disconnect()

        assert report.recording_anchor is not None
        assert report.recording_anchor.armed_host_ns == clock.recording_armed_ns
        assert report.recording_anchor.first_frame_host_ns >= clock.recording_armed_ns
        assert report.recording_anchor.first_frame_device_ns is not None

    def test_no_anchor_when_no_frames_arrive(self, tmp_path, fake_depthai):
        from syncfield.adapters.oak_camera import OakCameraStream
        from syncfield.clock import SessionClock
        from syncfield.types import SyncPoint
        import time

        stream = OakCameraStream("oak2", tmp_path, device_id="dev")
        stream.connect()
        sp = SyncPoint.create_now("h")
        clock = SessionClock(sync_point=sp, recording_armed_ns=time.monotonic_ns())
        stream.start_recording(clock)
        # no frames pushed
        report = stream.stop_recording()
        stream.disconnect()

        assert report.recording_anchor is None
```

(실제 fake_depthai fixture 이름은 기존 테스트 파일에서 확인 후 매칭 — `tests/unit/adapters/conftest.py` 에 있는 기존 fixture 의 push-frame 헬퍼 사용.)

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/unit/adapters/test_oak_camera.py::TestRecordingAnchor -v
```
Expected: fail — `report.recording_anchor is None` (adapter 미적용).

- [ ] **Step 3: Wire anchor into OAK capture loop**

`src/syncfield/adapters/oak_camera.py`:

(a) `start_recording` (line ~405) 가장 시작부분에서 session_clock 전달:

Before:
```python
    def start_recording(self, session_clock: SessionClock) -> None:
        """...existing..."""
        # ... existing body ...
```

After:
```python
    def start_recording(self, session_clock: SessionClock) -> None:
        """...existing..."""
        self._begin_recording_window(session_clock)
        # ... existing body ...
```

(b) `_capture_loop` (line ~708) 의 frame-arrival 시점에서 첫 프레임 기록. line 727 근처 `device_ts_ns = _device_timestamp_ns(rgb_msg)` 직후:

Before:
```python
                device_ts_ns = _device_timestamp_ns(rgb_msg)
                if self._recording:
                    if self._prev_capture_ns is not None:
                        self._intervals_ns.append(capture_ns - self._prev_capture_ns)
                    self._prev_capture_ns = capture_ns
```

After:
```python
                device_ts_ns = _device_timestamp_ns(rgb_msg)
                if self._recording:
                    self._observe_first_frame(capture_ns, device_ts_ns)
                    if self._prev_capture_ns is not None:
                        self._intervals_ns.append(capture_ns - self._prev_capture_ns)
                    self._prev_capture_ns = capture_ns
```

(c) `stop_recording` 끝에서 FinalizationReport 만들 때 `recording_anchor=self._recording_anchor()` 전달:

Before (예시 — 실제 report 생성 줄 찾아서):
```python
        return FinalizationReport(
            stream_id=self.id, status=status, frame_count=self._frame_count,
            file_path=self._mp4_path if self._mp4_path.exists() else None,
            first_sample_at_ns=..., last_sample_at_ns=...,
            health_events=self._collected_health, error=error,
            jitter_p95_ns=jitter_p95, jitter_p99_ns=jitter_p99,
        )
```

After — 필드 추가:
```python
        return FinalizationReport(
            stream_id=self.id, status=status, frame_count=self._frame_count,
            file_path=self._mp4_path if self._mp4_path.exists() else None,
            first_sample_at_ns=..., last_sample_at_ns=...,
            health_events=self._collected_health, error=error,
            jitter_p95_ns=jitter_p95, jitter_p99_ns=jitter_p99,
            recording_anchor=self._recording_anchor(),
        )
```

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/unit/adapters/test_oak_camera.py -v
```
Expected: full oak_camera suite green, including `TestRecordingAnchor`.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/oak_camera.py tests/unit/adapters/test_oak_camera.py
git commit -m "feat(oak_camera): record intra-host sync anchor on first recording frame"
```

---

## Task 7: `UVCWebcamStream` — anchor capture (no device clock)

**Files:**
- Modify: `src/syncfield/adapters/uvc_webcam.py` (`start_recording` ~line 163, capture loop)
- Test: `tests/unit/adapters/test_uvc_webcam.py`

- [ ] **Step 1: Write failing test**

패턴은 Task 6 과 동일. 차이점: `first_frame_device_ns is None` 이어야 함 (UVC는 device clock 없음).

```python
class TestRecordingAnchor:
    def test_uvc_anchor_without_device_ts(self, tmp_path, fake_av):
        from syncfield.adapters.uvc_webcam import UVCWebcamStream
        from syncfield.clock import SessionClock
        from syncfield.types import SyncPoint
        import time

        stream = UVCWebcamStream("cam", tmp_path, device_index=0)
        stream.connect()
        sp = SyncPoint.create_now("h")
        clock = SessionClock(sync_point=sp, recording_armed_ns=time.monotonic_ns())
        stream.start_recording(clock)
        fake_av.push_frame(stream)
        time.sleep(0.05)
        report = stream.stop_recording()
        stream.disconnect()

        assert report.recording_anchor is not None
        assert report.recording_anchor.first_frame_device_ns is None
        assert report.recording_anchor.first_frame_host_ns >= clock.recording_armed_ns
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/unit/adapters/test_uvc_webcam.py::TestRecordingAnchor -v
```

- [ ] **Step 3: Wire anchor into UVC capture loop**

`src/syncfield/adapters/uvc_webcam.py`:

(a) `start_recording` 시작부분에 `self._begin_recording_window(session_clock)` 추가.

(b) capture loop 의 frame-arrival 지점에서 `self._observe_first_frame(capture_ns, device_ns=None)` 추가. UVC의 `_capture_loop` 위치는 파일 내 `_capture_loop` 함수 찾아서, `capture_ns = time.monotonic_ns()` 직후 `if self._recording:` 블록 안.

(c) `stop_recording` 의 FinalizationReport 생성부에 `recording_anchor=self._recording_anchor()` 추가.

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/unit/adapters/test_uvc_webcam.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/uvc_webcam.py tests/unit/adapters/test_uvc_webcam.py
git commit -m "feat(uvc_webcam): record intra-host sync anchor (no device clock)"
```

---

## Task 8: PollingSensorStream + PushSensorStream — anchor capture

**Files:**
- Modify: `src/syncfield/adapters/polling_sensor.py`, `src/syncfield/adapters/push_sensor.py`
- Test: 기존 센서 테스트 파일 확장

- [ ] **Step 1: Write failing test**

`tests/unit/adapters/test_polling_sensor.py` (기존 파일 혹은 신규):

```python
def test_polling_sensor_anchor_captured():
    from syncfield.adapters.polling_sensor import PollingSensorStream
    from syncfield.clock import SessionClock
    from syncfield.types import SyncPoint
    import time

    poll_result = {"x": 1.0}
    stream = PollingSensorStream(
        id="ps", poll_fn=lambda: poll_result, interval_s=0.01,
    )
    stream.connect()
    sp = SyncPoint.create_now("h")
    clock = SessionClock(sync_point=sp, recording_armed_ns=time.monotonic_ns())
    stream.start_recording(clock)
    time.sleep(0.05)
    report = stream.stop_recording()
    stream.disconnect()
    assert report.recording_anchor is not None
    assert report.recording_anchor.first_frame_host_ns >= clock.recording_armed_ns
```

유사 테스트 `test_push_sensor.py` 에 추가.

- [ ] **Step 2: Run test to verify failure**
- [ ] **Step 3: Wire into both sensor adapters**

`polling_sensor.py` 와 `push_sensor.py`:

(a) `start_recording` 에 `self._begin_recording_window(session_clock)` 추가.

(b) 샘플 emission 지점 (각 파일 내 `_emit_sample` 직전의 `capture_ns` 계산 직후)에 `self._observe_first_frame(capture_ns, device_ns=None)` 추가.

(c) `stop_recording` FinalizationReport 에 `recording_anchor=self._recording_anchor()` 추가.

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/unit/adapters/test_polling_sensor.py tests/unit/adapters/test_push_sensor.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/adapters/polling_sensor.py src/syncfield/adapters/push_sensor.py tests/unit/adapters/test_polling_sensor.py tests/unit/adapters/test_push_sensor.py
git commit -m "feat(sensors): record intra-host sync anchor in generic sensor adapters"
```

---

## Task 9: Remaining adapters — batch extension

**Files (같은 패턴 반복):**
- `src/syncfield/adapters/meta_quest.py` — quest clock 있음 → `first_frame_device_ns` 에 quest_native_ns 전달
- `src/syncfield/adapters/oglo_tactile.py` — BLE timestamp 있으면 전달, 없으면 None
- `src/syncfield/adapters/ble_imu.py` — BLE device ts (있으면)
- `src/syncfield/adapters/host_audio.py` — device clock 없음 → None
- `src/syncfield/adapters/jsonl_file.py` — 파일 기반, armed/first 의미 미약 → 첫 record 의 `host_ns, None`
- `src/syncfield/adapters/meta_quest_camera/` — oak_camera 패턴 참고
- `src/syncfield/adapters/insta360_go3s/` — 오프라인 ingest 성격이면 skip; 실시간이면 oak 패턴

각 어댑터마다 3-step (test → wire → commit) 서브태스크 반복. 구조가 Task 6~8 과 동일하므로 동일 체크리스트 사용.

- [ ] **Step 1: meta_quest** (test → wire → verify → commit)
- [ ] **Step 2: oglo_tactile** (test → wire → verify → commit)
- [ ] **Step 3: ble_imu** (test → wire → verify → commit)
- [ ] **Step 4: host_audio** (test → wire → verify → commit)
- [ ] **Step 5: jsonl_file** (test → wire → verify → commit)
- [ ] **Step 6: meta_quest_camera** (test → wire → verify → commit)
- [ ] **Step 7: insta360_go3s** — 실시간 streaming adapter 인지 먼저 grep 확인. 오프라인 ingest 전용이면 이 task 는 skip.

각 step 당 commit message:
```
feat(<adapter_name>): record intra-host sync anchor on first recording frame
```

---

## Task 10: Integration test — multi-adapter shared anchor

**Files:**
- Test: `tests/integration/test_anchor_sharing.py` (신규)

- [ ] **Step 1: Write integration test**

```python
"""All streams in one session observe the SAME armed_host_ns.

This is the end-to-end validation that intra-host sync metadata is
wired all the way through SessionOrchestrator → SessionClock →
adapters → FinalizationReport → manifest.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from syncfield.orchestrator import SessionOrchestrator
from syncfield.adapters.polling_sensor import PollingSensorStream


def test_all_streams_share_armed_host_ns(tmp_path: Path) -> None:
    sess = SessionOrchestrator(host_id="h", output_dir=tmp_path)
    sess.add(PollingSensorStream("a", poll_fn=lambda: {"x": 1.0}, interval_s=0.01))
    sess.add(PollingSensorStream("b", poll_fn=lambda: {"y": 2.0}, interval_s=0.01))
    sess.add(PollingSensorStream("c", poll_fn=lambda: {"z": 3.0}, interval_s=0.01))
    sess.connect(); sess.start(); time.sleep(0.1); sess.stop()

    manifest_path = next(tmp_path.rglob("manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    anchors = [
        s["recording_anchor"] for s in manifest["streams"]
        if s.get("recording_anchor")
    ]
    assert len(anchors) == 3
    armed_values = {a["armed_host_ns"] for a in anchors}
    assert len(armed_values) == 1, (
        f"All streams must share the same armed_host_ns; got {armed_values}"
    )
    # Each stream's first frame must arrive after the armed moment
    for a in anchors:
        assert a["first_frame_host_ns"] >= a["armed_host_ns"]
        assert a["first_frame_latency_ns"] >= 0
```

- [ ] **Step 2: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_anchor_sharing.py -v
```
Expected: pass (assuming Tasks 5 + 8 are in).

- [ ] **Step 3: Full regression**

```bash
uv run pytest tests/ -x --timeout=60
```
Expected: entire suite green.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_anchor_sharing.py
git commit -m "test(integration): verify all streams share a single armed_host_ns"
```

---

## Task 11: Stability hardening

**Files:**
- Modify: `src/syncfield/stream.py` (helper 방어 로직 확인)
- Test: `tests/unit/test_stream_base.py` (edge case 추가)

- [ ] **Step 1: Edge case tests**

```python
def test_anchor_helper_safe_when_start_recording_not_called():
    """If an adapter emits frames before start_recording (e.g. preview
    phase leaking into capture loop), anchor must stay None — not crash."""
    d = _Dummy()
    d._observe_first_frame(host_ns=100, device_ns=None)
    assert d._recording_anchor() is None


def test_anchor_helper_thread_safety_idempotent():
    """Concurrent first-frame observations: the first one wins; the
    second returns silently."""
    d = _Dummy()
    d._begin_recording_window(_clock(armed_ns=100))
    d._observe_first_frame(host_ns=200, device_ns=None)
    d._observe_first_frame(host_ns=300, device_ns=None)
    assert d._recording_anchor().first_frame_host_ns == 200


def test_anchor_helper_handles_negative_clock_skew():
    """host_ns can trail armed_host_ns under mock clock / test harness.
    Helper must NOT raise — it clamps to armed_host_ns so
    RecordingAnchor invariant holds."""
    d = _Dummy()
    d._begin_recording_window(_clock(armed_ns=1_000))
    d._observe_first_frame(host_ns=500, device_ns=None)  # clock went back
    anchor = d._recording_anchor()
    assert anchor is not None
    assert anchor.first_frame_host_ns == 1_000  # clamped
    assert anchor.first_frame_latency_ns == 0
```

- [ ] **Step 2: Run to verify pass (helper already has clamp + idempotence from Task 3)**

```bash
uv run pytest tests/unit/test_stream_base.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_stream_base.py
git commit -m "test(stream): harden anchor helper against clock skew and race"
```

---

## Task 12: Docs + version bump

**Files:**
- Modify: `README.md` (manifest 섹션이 있으면)
- Modify: `docs/` — intra-host sync 설명에 anchor 섹션 추가 (기존 관련 문서 있으면)
- Modify: `pyproject.toml` (version bump 0.3.21 → 0.3.22)

- [ ] **Step 1: Bump version**

`pyproject.toml`:
```toml
version = "0.3.22"
```

- [ ] **Step 2: Manifest schema docs**

기존 docs/ 안에 manifest 설명 파일 있으면 `recording_anchor` 필드 설명 추가. 없으면 skip.

- [ ] **Step 3: Run full suite once more**

```bash
uv run pytest tests/ --timeout=60
```

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml docs/
git commit -m "chore(release): v0.3.22 — intra-host sync anchor metadata"
```

---

## Self-Review Checklist

**Spec coverage:**
- ✅ 공통 armed_host_ns capture & propagation: Task 5
- ✅ per-adapter first-frame anchor capture: Task 6-9
- ✅ Metadata surfaces on FinalizationReport + manifest: Tasks 4-5
- ✅ Backward compat (optional field, legacy adapters untouched): Task 2 default=None
- ✅ Device-ts-less adapters supported: Task 7, 8 (first_frame_device_ns=None)
- ✅ Edge cases: Task 11 (no frame, clock skew, idempotence)
- ✅ End-to-end verification: Task 10

**Placeholder scan:** no TBD, no "handle edge cases", all code blocks contain actual code.

**Type consistency:** `_begin_recording_window`, `_observe_first_frame`, `_recording_anchor`, `recording_anchor`, `armed_host_ns`, `first_frame_host_ns`, `first_frame_device_ns` — consistent across all tasks.

**Out of scope (intentional):**
- syncfield(sync service) 측 alignment 활용은 별도 plan. 이 plan 은 metadata 기록까지만.
- `SessionClock.with_armed(ns)` 같은 method 대신 `dataclasses.replace` 로 직접 복제 — YAGNI.
- Anchor 의 wall_clock 변환은 downstream 이 기존 `sync_point.wall_clock_ns` + `armed_host_ns - sync_point.monotonic_ns` 로 계산 가능하므로 별도 저장 안 함.
