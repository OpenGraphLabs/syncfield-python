# Insta360 Go3S Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Insta360 Go3S camera adapter to syncfield-python that triggers recording over BLE and retrieves files over WiFi as a background, atomic, per-episode job — without blocking the recording lifecycle.

**Architecture:** Two-phase atomic semantics — `recording` (BLE start/stop) is synchronous in the session; `aggregation` (WiFi switch + OSC HTTP download) runs in a process-wide singleton background queue, decoupled from session state. Per-episode atomic: all cameras succeed or the episode is `FAILED` with originals preserved on the camera SD for retry. Multihost leader/follower roles auto-downgrade to `on_demand` aggregation to avoid breaking lab WiFi.

**Tech Stack:** Python 3.11+, `bleak` (BLE), `aiohttp` (HTTP/OSC), platform CLIs (`networksetup` on macOS, `nmcli` on Linux). Frontend: existing React + TypeScript viewer.

**Reference sources to port (read-only inputs):**
- `/Users/jerry/Documents/opengraph-studio/recorder/src/syncfield_recorder/sensors/insta360_ble/protocol.py`
- `/Users/jerry/Documents/opengraph-studio/recorder/src/syncfield_recorder/sensors/insta360_ble/camera.py`
- `/Users/jerry/Documents/opengraph-studio/recorder/src/syncfield_recorder/sensors/insta360_go3s_sensor.py`
- `/Users/jerry/Documents/opengraph-studio/recorder/scripts/download_go3s_wifi.py`

**Spec:** `docs/superpowers/specs/2026-04-14-insta360-go3s-adapter-design.md`

---

## File Structure

### New (backend)

```
src/syncfield/adapters/insta360_go3s/
├── __init__.py                  # public re-export of Go3SStream
├── stream.py                    # Go3SStream (StreamBase subclass)
├── ble/
│   ├── __init__.py
│   ├── protocol.py              # FFFrame, CRC-16/MODBUS, command builders
│   └── camera.py                # Go3SBLECamera (async helper)
├── wifi/
│   ├── __init__.py
│   ├── osc_client.py            # OscHttpClient (aiohttp)
│   └── switcher.py              # WifiSwitcher ABC + Mac/Linux/Windows impls
└── aggregation/
    ├── __init__.py
    ├── types.py                 # AggregationState, AggregationProgress
    └── queue.py                 # AggregationQueue singleton + worker
```

### Modified (backend)

- `src/syncfield/types.py` — add `live_preview` to `StreamCapabilities`; widen `FinalizationReport.status` union.
- `src/syncfield/adapters/__init__.py` — lazy re-export of Go3SStream.
- `src/syncfield/orchestrator.py` — multihost role detection → policy downgrade; aggregation event forwarding to viewer snapshot.
- `src/syncfield/viewer/server.py` — extend WS snapshot; new control commands.
- `pyproject.toml` — add `aiohttp` to `camera` optional extra.

### New (frontend)

- `src/syncfield/viewer/frontend/src/components/standalone-recorder-panel.tsx`
- `src/syncfield/viewer/frontend/src/components/aggregation-status-bar.tsx`

### Modified (frontend)

- `src/syncfield/viewer/frontend/src/components/stream-card.tsx` — dispatcher routing for `live_preview === false`.
- `src/syncfield/viewer/frontend/src/components/discovery-modal.tsx` — Go3S name pattern recognition.
- Episode list component (locate during Task 14) — add aggregation status badge.

### Tests

```
tests/unit/adapters/insta360_go3s/
├── __init__.py
├── test_ble_protocol.py
├── test_ble_camera.py
├── test_osc_client.py
├── test_wifi_switcher.py
├── test_aggregation_queue.py
└── test_go3s_stream.py

tests/integration/insta360_go3s/
├── __init__.py
├── test_session_e2e.py
├── test_aggregation_during_recording.py
└── test_atomic_failure.py
```

---

## Phase A — Backend Foundations

### Task 1: Extend `StreamCapabilities` and `FinalizationReport`

**Files:**
- Modify: `src/syncfield/types.py` (lines around 164–189 and 277–307)
- Test: `tests/unit/test_types_capabilities.py` (create if missing) and `tests/unit/test_types_finalization.py`

- [ ] **Step 1: Write failing test for `live_preview` default and serialization**

Create `tests/unit/test_types_capabilities.py` (or append to nearest existing types test):

```python
from syncfield.types import StreamCapabilities


def test_live_preview_defaults_to_true():
    caps = StreamCapabilities()
    assert caps.live_preview is True


def test_live_preview_can_be_disabled():
    caps = StreamCapabilities(live_preview=False)
    assert caps.live_preview is False


def test_to_dict_includes_live_preview():
    caps = StreamCapabilities(live_preview=False)
    d = caps.to_dict()
    assert d["live_preview"] is False
    assert d["produces_file"] is False
```

- [ ] **Step 2: Run the test to confirm it fails**

```
uv run pytest tests/unit/test_types_capabilities.py -v
```
Expected: AttributeError or AssertionError on `live_preview`.

- [ ] **Step 3: Add `live_preview` to `StreamCapabilities`**

Edit `src/syncfield/types.py` at the `StreamCapabilities` definition:

```python
@dataclass
class StreamCapabilities:
    """What a Stream declares it can provide."""

    provides_audio_track: bool = False
    supports_precise_timestamps: bool = False
    is_removable: bool = False
    produces_file: bool = False
    live_preview: bool = True  # False = viewer renders standalone placeholder

    def to_dict(self) -> dict[str, Any]:
        return {
            "provides_audio_track": self.provides_audio_track,
            "supports_precise_timestamps": self.supports_precise_timestamps,
            "is_removable": self.is_removable,
            "produces_file": self.produces_file,
            "live_preview": self.live_preview,
        }
```

Update the docstring `Attributes:` block to include `live_preview`.

- [ ] **Step 4: Write failing test for new FinalizationReport status**

Append to `tests/unit/test_types_finalization.py` (create file if missing):

```python
from pathlib import Path
from syncfield.types import FinalizationReport


def test_finalization_report_accepts_pending_aggregation_status():
    report = FinalizationReport(
        stream_id="overhead",
        status="pending_aggregation",
        frame_count=0,
        file_path=None,
        first_sample_at_ns=None,
        last_sample_at_ns=None,
        health_events=[],
        error=None,
    )
    assert report.status == "pending_aggregation"
```

- [ ] **Step 5: Run to confirm it fails (type-narrowing or runtime issues)**

```
uv run pytest tests/unit/test_types_finalization.py -v
```
At runtime the dataclass accepts any string, but `mypy`/`pyright` flag invalid Literal. Run pyright too:

```
uv run pyright src/syncfield/types.py tests/unit/test_types_finalization.py
```

Expected: pyright error "Argument of type 'pending_aggregation' is not assignable to ...".

- [ ] **Step 6: Widen the status union**

Edit `FinalizationReport.status` annotation in `src/syncfield/types.py`:

```python
status: Literal["completed", "partial", "failed", "pending_aggregation"]
```

Update the docstring's Attributes section to mention `pending_aggregation`: "stream finished its synchronous lifecycle but a background aggregation job is still required to land all artifacts on disk".

- [ ] **Step 7: Run all tests**

```
uv run pytest tests/unit/test_types_capabilities.py tests/unit/test_types_finalization.py -v
uv run pyright src/syncfield/types.py
```
Expected: PASS, no pyright errors.

- [ ] **Step 8: Commit**

```
git add src/syncfield/types.py tests/unit/test_types_capabilities.py tests/unit/test_types_finalization.py
git commit -m "feat(types): add live_preview capability and pending_aggregation status"
```

---

### Task 2: Create Go3S package skeleton

**Files:**
- Create: `src/syncfield/adapters/insta360_go3s/__init__.py`
- Create: `src/syncfield/adapters/insta360_go3s/ble/__init__.py`
- Create: `src/syncfield/adapters/insta360_go3s/wifi/__init__.py`
- Create: `src/syncfield/adapters/insta360_go3s/aggregation/__init__.py`
- Create: `tests/unit/adapters/insta360_go3s/__init__.py`
- Create: `tests/integration/insta360_go3s/__init__.py`

- [ ] **Step 1: Create empty package files**

For each `__init__.py` listed above, create with content:

```python
"""Insta360 Go3S adapter (BLE trigger + WiFi aggregation)."""
```

For sub-package files (`ble/__init__.py`, `wifi/__init__.py`, `aggregation/__init__.py`), use the sub-name in the docstring (`"""BLE protocol and camera control for Insta360 Go3S."""`, etc.).

For the test `__init__.py` files, leave empty (no content).

- [ ] **Step 2: Verify package importable**

```
uv run python -c "import syncfield.adapters.insta360_go3s; import syncfield.adapters.insta360_go3s.ble; import syncfield.adapters.insta360_go3s.wifi; import syncfield.adapters.insta360_go3s.aggregation; print('ok')"
```
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```
git add src/syncfield/adapters/insta360_go3s tests/unit/adapters/insta360_go3s tests/integration/insta360_go3s
git commit -m "feat(adapters): scaffold insta360_go3s package"
```

---

### Task 3: Port BLE protocol module (FFFrame + CRC + command builders)

**Files:**
- Reference (read-only): `/Users/jerry/Documents/opengraph-studio/recorder/src/syncfield_recorder/sensors/insta360_ble/protocol.py`
- Create: `src/syncfield/adapters/insta360_go3s/ble/protocol.py`
- Test: `tests/unit/adapters/insta360_go3s/test_ble_protocol.py`

- [ ] **Step 1: Write failing test for CRC-16/MODBUS reference vector**

Create `tests/unit/adapters/insta360_go3s/test_ble_protocol.py`:

```python
import pytest

from syncfield.adapters.insta360_go3s.ble import protocol as p


# CRC-16/MODBUS reference vectors
# (https://crccalc.com/?crc=...&method=crc16&datatype=hex&outtype=hex)
@pytest.mark.parametrize(
    "data,expected",
    [
        (b"\x01\x02\x03\x04", 0x2BA1),
        (b"123456789", 0x4B37),
        (b"\xff", 0x40BF),
    ],
)
def test_crc16_modbus_known_vectors(data, expected):
    assert p.crc16_modbus(data) == expected


def test_crc16_modbus_empty():
    assert p.crc16_modbus(b"") == 0xFFFF


def test_constants_match_protocol_spec():
    assert p.SERVICE_UUID == "0000be80-0000-1000-8000-00805f9b34fb"
    assert p.WRITE_CHAR_UUID == "0000be81-0000-1000-8000-00805f9b34fb"
    assert p.NOTIFY_CHAR_UUID == "0000be82-0000-1000-8000-00805f9b34fb"
    assert p.CMD_START_CAPTURE == 0x0004
    assert p.CMD_STOP_CAPTURE == 0x0005
    assert p.CMD_CHECK_AUTH == 0x0027
    assert p.CMD_SET_OPTIONS == 0x0002
    assert p.STATUS_OK == 0x00C8


def test_build_message_packet_structure():
    """A message packet starts with FF, type 0x07 (app->cam), subtype 0x40.

    Inner header is 16 bytes; CRC-16 trails."""
    pkt = p.build_message_packet(
        cmd=p.CMD_START_CAPTURE,
        seq=1,
        protobuf_payload=b"\x08\x01",
    )
    assert pkt[0] == 0xFF
    assert pkt[1] == 0x07
    assert pkt[2] == 0x40
    # last 2 bytes are CRC-16 little-endian
    body = pkt[:-2]
    crc = int.from_bytes(pkt[-2:], "little")
    assert crc == p.crc16_modbus(body)


def test_build_sync_response_is_constant():
    """SYNC response is fixed bytes + CRC."""
    sync = p.build_sync_response()
    assert sync[0] == 0xFF
    assert sync[1] == 0x07
    assert sync[2] == 0x41  # SUBTYPE_SYNC


def test_build_check_auth_payload_format():
    """auth_id is wrapped: [0x0A, len(addr)] + addr + [0x10, 0x02]."""
    addr = b"AA:BB:CC:DD:EE:FF"
    pb = p.build_check_auth_payload(addr)
    assert pb[0] == 0x0A
    assert pb[1] == len(addr)
    assert pb[2 : 2 + len(addr)] == addr
    assert pb[-2:] == b"\x10\x02"


def test_parse_response_extracts_seq_and_status():
    """A camera response (type 0x06) carries seq at byte 10, status at bytes 11-12 LE."""
    fake = (
        bytes([0xFF, 0x06, 0x40])
        + (16).to_bytes(2, "little")  # payload_len placeholder
        + bytes(
            [
                0x00, 0x00, 0x00, 0x00,        # bytes 0-3 of inner header
                0x04,                           # mode
                0x00, 0x00,                     # bytes 5-6
                0x04, 0x00,                     # cmd_code (LE)
                0x02,                           # content_type
                0x01,                           # seq <- byte 10
                0xC8, 0x00,                     # status_code (LE) = 200
                0x00,                           # direction
                0x00, 0x00,                     # bytes 14-15
            ]
        )
    )
    pkt = fake + p.crc16_modbus(fake).to_bytes(2, "little")
    parsed = p.parse_response_packet(pkt)
    assert parsed.seq == 1
    assert parsed.status == p.STATUS_OK
    assert parsed.cmd == p.CMD_START_CAPTURE
```

- [ ] **Step 2: Run to confirm failure**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_ble_protocol.py -v
```
Expected: ImportError / module not found.

- [ ] **Step 3: Port the protocol module**

Read `/Users/jerry/Documents/opengraph-studio/recorder/src/syncfield_recorder/sensors/insta360_ble/protocol.py` end-to-end. Port to `src/syncfield/adapters/insta360_go3s/ble/protocol.py` verbatim, with these changes:

- Update the module docstring to reference `xaionaro-go/insta360ctl` and the recorder source as origins.
- Keep all constants identical (UUIDs, command codes, status codes).
- Keep `crc16_modbus`, the FFFrame builder/parser, command-builder functions identical.
- Ensure exported names match what tests in Step 1 import: `crc16_modbus`, `build_message_packet`, `build_sync_response`, `build_check_auth_payload`, `parse_response_packet`, `ParsedResponse` (the dataclass used by `parse_response_packet`).

If the recorder uses different function names (e.g. `encode_frame` instead of `build_message_packet`), prefer the names used in the tests above and add a thin alias if needed for clarity. The semantic behavior must match the recorder bit-for-bit.

- [ ] **Step 4: Run tests to confirm pass**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_ble_protocol.py -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```
git add src/syncfield/adapters/insta360_go3s/ble/protocol.py tests/unit/adapters/insta360_go3s/test_ble_protocol.py
git commit -m "feat(go3s/ble): port FFFrame protocol from recorder"
```

---

### Task 4: Port `Go3SBLECamera` async helper

**Files:**
- Reference (read-only): `/Users/jerry/Documents/opengraph-studio/recorder/src/syncfield_recorder/sensors/insta360_ble/camera.py`
- Create: `src/syncfield/adapters/insta360_go3s/ble/camera.py`
- Test: `tests/unit/adapters/insta360_go3s/test_ble_camera.py`

- [ ] **Step 1: Write failing test using a fake BLE backend**

Create `tests/unit/adapters/insta360_go3s/test_ble_camera.py`:

```python
import asyncio
import time
from collections import deque
from typing import Callable

import pytest

from syncfield.adapters.insta360_go3s.ble import protocol as p
from syncfield.adapters.insta360_go3s.ble.camera import (
    CaptureResult,
    Go3SBLECamera,
)


class FakeBleakClient:
    """Minimal in-memory bleak.BleakClient stand-in.

    Records writes; when a CMD packet arrives, queues the matching
    response for the next notify dispatch.
    """

    def __init__(self, address: str):
        self.address = address
        self.is_connected = False
        self._notify_cb: Callable[[int, bytearray], None] | None = None
        self._write_log: list[bytes] = []
        self._pending_notifications: deque[bytes] = deque()

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, char_uuid, callback):
        assert char_uuid == p.NOTIFY_CHAR_UUID
        self._notify_cb = callback
        # Send SYNC immediately so the camera's connect() doesn't time out
        sync = bytes([0xFF, 0x06, 0x41]) + b"\x07\x00" + b"\x00" * 9
        sync = sync + p.crc16_modbus(sync).to_bytes(2, "little")
        await asyncio.sleep(0)  # let subscriber wire up
        callback(0, bytearray(sync))

    async def stop_notify(self, char_uuid):
        self._notify_cb = None

    async def write_gatt_char(self, char_uuid, data, response=True):
        assert char_uuid == p.WRITE_CHAR_UUID
        self._write_log.append(bytes(data))
        # If this is a CMD message, fabricate a STATUS_OK response with same seq
        parsed = p.parse_request_packet(bytes(data)) if hasattr(p, "parse_request_packet") else None
        if parsed is not None and parsed.cmd in (
            p.CMD_CHECK_AUTH,
            p.CMD_START_CAPTURE,
            p.CMD_STOP_CAPTURE,
            p.CMD_SET_OPTIONS,
        ):
            resp = self._build_ok_response(parsed.cmd, parsed.seq, parsed.cmd == p.CMD_STOP_CAPTURE)
            assert self._notify_cb is not None
            self._notify_cb(0, bytearray(resp))

    @staticmethod
    def _build_ok_response(cmd: int, seq: int, with_filename: bool) -> bytes:
        inner = bytearray(16)
        inner[4] = 0x04                 # mode
        inner[7:9] = cmd.to_bytes(2, "little")
        inner[9] = 0x02                 # content_type
        inner[10] = seq
        inner[11:13] = p.STATUS_OK.to_bytes(2, "little")
        pb = b""
        if with_filename:
            name = b"/DCIM/Camera01/VID_FAKE.mp4"
            pb = b"\x12" + len(name).to_bytes(1, "big") + name
        payload = bytes(inner) + pb
        outer = (
            bytes([0xFF, 0x06, 0x40])
            + len(payload).to_bytes(2, "little")
            + payload
        )
        return outer + p.crc16_modbus(outer).to_bytes(2, "little")


@pytest.fixture
def fake_client(monkeypatch):
    instances: list[FakeBleakClient] = []

    def factory(address, *args, **kwargs):
        c = FakeBleakClient(address)
        instances.append(c)
        return c

    monkeypatch.setattr(
        "syncfield.adapters.insta360_go3s.ble.camera.BleakClient",
        factory,
    )
    return instances


@pytest.mark.asyncio
async def test_connect_runs_sync_and_auth(fake_client):
    cam = Go3SBLECamera(address="AA:BB:CC:DD:EE:FF")
    await cam.connect(sync_timeout=2.0, auth_timeout=2.0)
    assert fake_client[0].is_connected
    # First write should be SYNC response (subtype 0x41)
    first = fake_client[0]._write_log[0]
    assert first[2] == 0x41
    # Second write should be CHECK_AUTH (cmd 0x0027)
    second = fake_client[0]._write_log[1]
    inner_cmd = int.from_bytes(second[5 + 2 : 5 + 2 + 2], "little")  # offset depends on FFFrame layout
    # less brittle: at least one write encodes CHECK_AUTH
    cmd_codes = {
        int.from_bytes(w[5 + 2 : 5 + 2 + 2], "little")
        for w in fake_client[0]._write_log
        if w[2] == 0x40
    }
    assert p.CMD_CHECK_AUTH in cmd_codes
    await cam.disconnect()


@pytest.mark.asyncio
async def test_start_capture_returns_host_ns(fake_client):
    cam = Go3SBLECamera(address="AA:BB:CC:DD:EE:FF")
    await cam.connect()
    before = time.monotonic_ns()
    ack_ns = await cam.start_capture()
    after = time.monotonic_ns()
    assert before <= ack_ns <= after
    await cam.disconnect()


@pytest.mark.asyncio
async def test_stop_capture_returns_filepath(fake_client):
    cam = Go3SBLECamera(address="AA:BB:CC:DD:EE:FF")
    await cam.connect()
    await cam.start_capture()
    result: CaptureResult = await cam.stop_capture()
    assert result.file_path == "/DCIM/Camera01/VID_FAKE.mp4"
    await cam.disconnect()
```

- [ ] **Step 2: Run to confirm failure**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_ble_camera.py -v
```
Expected: ImportError on `Go3SBLECamera` / `CaptureResult`.

- [ ] **Step 3: Port the camera helper**

Read `/Users/jerry/Documents/opengraph-studio/recorder/src/syncfield_recorder/sensors/insta360_ble/camera.py` end-to-end. Port to `src/syncfield/adapters/insta360_go3s/ble/camera.py` with these requirements:

- Public API:
  ```python
  @dataclass
  class CaptureResult:
      file_path: str
      ack_host_ns: int

  class Go3SBLECamera:
      def __init__(self, address: str): ...
      async def connect(self, *, sync_timeout: float = 2.0, auth_timeout: float = 1.0) -> None: ...
      async def set_video_mode(self) -> None: ...
      async def start_capture(self) -> int: ...   # returns ack_host_ns (time.monotonic_ns())
      async def stop_capture(self) -> CaptureResult: ...
      async def disconnect(self) -> None: ...
      @property
      def is_connected(self) -> bool: ...
  ```
- Keep the recorder's connect-send-disconnect pattern: `connect()` performs SYNC handshake + `CMD_CHECK_AUTH`, then leaves the BLE link open for callers to issue commands. (Note: stream-level `connect()` will close BLE after auth; per-command reconnect is at the stream layer in Task 10.)
- Use `bleak.BleakClient` for the GATT connection. Import as `from bleak import BleakClient` so the test's monkeypatch works.
- All command writes go through `_send(cmd, payload, timeout=2.0)` which constructs the FFFrame, writes to `WRITE_CHAR_UUID`, awaits a notify with matching `seq`, and validates `STATUS_OK`.
- Sequence counter starts at 1 and wraps 1..254 (skip 0 and 255). Maintain it as an instance attribute.
- `start_capture` payload is `pb=b"\x08\x01"` (mode=1=normal). `stop_capture` payload is empty `b""`. `set_video_mode` payload is `pb=bytes([0x0A, 0x04, 0x08, 0x29, 0x10, 0x00])` (video submode=0).
- `stop_capture` parses the response payload for the file path: scan for ASCII `/DCIM/` substring up to first NUL or whitespace, accept `.mp4`/`.insv`/`.lrv` extensions.
- If the test's monkeypatch breaks because the recorder uses a slightly different signature for write_gatt_char etc., adjust the test to match the recorder's public API while keeping intent (assert the auth + capture commands fired).

If the recorder defines `parse_request_packet`, also export it from `protocol.py` (the test imports it conditionally). Otherwise add it: a parser symmetric to `parse_response_packet` that decodes app→cam packets.

- [ ] **Step 4: Run tests to confirm pass**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_ble_camera.py -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```
git add src/syncfield/adapters/insta360_go3s/ble/camera.py tests/unit/adapters/insta360_go3s/test_ble_camera.py src/syncfield/adapters/insta360_go3s/ble/protocol.py
git commit -m "feat(go3s/ble): port async BLE camera helper"
```

---

### Task 5: Build `OscHttpClient`

**Files:**
- Reference (read-only): `/Users/jerry/Documents/opengraph-studio/recorder/scripts/download_go3s_wifi.py`
- Create: `src/syncfield/adapters/insta360_go3s/wifi/osc_client.py`
- Test: `tests/unit/adapters/insta360_go3s/test_osc_client.py`

- [ ] **Step 1: Write failing test using `aiohttp` test utilities**

Create `tests/unit/adapters/insta360_go3s/test_osc_client.py`:

```python
import json
from pathlib import Path

import pytest
from aiohttp import web

from syncfield.adapters.insta360_go3s.wifi.osc_client import (
    OscDownloadError,
    OscHttpClient,
)


@pytest.fixture
async def osc_server(aiohttp_unused_port, aiohttp_server):
    """Fake OSC HTTP server that mimics the Go3S endpoints we hit."""

    async def info(request):
        return web.json_response(
            {"manufacturer": "Insta360", "model": "Go 3S", "firmwareVersion": "8.0.4.11"}
        )

    async def execute(request):
        body = await request.json()
        if body["name"] == "camera.listFiles":
            return web.json_response(
                {
                    "results": {
                        "entries": [
                            {
                                "name": "VID_FAKE.mp4",
                                "fileUrl": "/DCIM/Camera01/VID_FAKE.mp4",
                                "size": 12,
                            }
                        ]
                    },
                    "state": "done",
                }
            )
        return web.json_response({"state": "error"}, status=400)

    async def get_file(request):
        return web.Response(body=b"hello world!", headers={"Content-Length": "12"})

    app = web.Application()
    app.router.add_get("/osc/info", info)
    app.router.add_post("/osc/commands/execute", execute)
    app.router.add_get("/DCIM/Camera01/VID_FAKE.mp4", get_file)
    return await aiohttp_server(app)


@pytest.mark.asyncio
async def test_probe_returns_camera_model(osc_server):
    client = OscHttpClient(host=f"127.0.0.1:{osc_server.port}", scheme="http")
    info = await client.probe(timeout=2.0)
    assert info.model == "Go 3S"


@pytest.mark.asyncio
async def test_list_files_returns_entries(osc_server):
    client = OscHttpClient(host=f"127.0.0.1:{osc_server.port}", scheme="http")
    files = await client.list_files()
    assert len(files) == 1
    assert files[0].name == "VID_FAKE.mp4"
    assert files[0].size == 12


@pytest.mark.asyncio
async def test_download_writes_atomic_file(osc_server, tmp_path):
    client = OscHttpClient(host=f"127.0.0.1:{osc_server.port}", scheme="http")
    target = tmp_path / "overhead.mp4"
    progress_calls: list[tuple[int, int]] = []

    await client.download(
        remote_path="/DCIM/Camera01/VID_FAKE.mp4",
        local_path=target,
        expected_size=12,
        on_progress=lambda done, total: progress_calls.append((done, total)),
    )

    assert target.exists()
    assert target.read_bytes() == b"hello world!"
    assert not (tmp_path / "overhead.mp4.part").exists()
    assert progress_calls[-1] == (12, 12)


@pytest.mark.asyncio
async def test_download_size_mismatch_raises_and_cleans_up(osc_server, tmp_path):
    client = OscHttpClient(host=f"127.0.0.1:{osc_server.port}", scheme="http")
    target = tmp_path / "overhead.mp4"
    with pytest.raises(OscDownloadError):
        await client.download(
            remote_path="/DCIM/Camera01/VID_FAKE.mp4",
            local_path=target,
            expected_size=99999,  # wrong size triggers atomic failure
        )
    assert not target.exists()
    assert not (tmp_path / "overhead.mp4.part").exists()
```

Add `aiohttp` and `pytest-aiohttp` to dev deps if missing. Verify pyproject:

```
uv run python -c "import pytest_aiohttp" || true
```

- [ ] **Step 2: Run to confirm failure**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_osc_client.py -v
```
Expected: ImportError on `OscHttpClient`.

- [ ] **Step 3: Implement `OscHttpClient`**

Create `src/syncfield/adapters/insta360_go3s/wifi/osc_client.py`:

```python
"""OSC (Open Spherical Camera) HTTP client for Insta360 Go3S.

Targets the Go3S AP (default 192.168.42.1). Endpoints mirror the public
OSC spec: ``/osc/info``, ``/osc/commands/execute`` (``camera.listFiles``),
plus direct file GETs on the SD card paths the camera reports.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import aiohttp


DEFAULT_HOST = "192.168.42.1"
FALLBACK_PORTS: tuple[int, ...] = (80, 6666, 8080)
PROGRESS_CHUNK = 64 * 1024


class OscDownloadError(RuntimeError):
    """Raised when an OSC file download cannot be completed atomically."""


@dataclass(frozen=True)
class OscCameraInfo:
    manufacturer: str
    model: str
    firmware_version: str


@dataclass(frozen=True)
class OscFileEntry:
    name: str
    file_url: str
    size: int


class OscHttpClient:
    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        scheme: str = "http",
        request_timeout: float = 10.0,
    ):
        self._host = host
        self._scheme = scheme
        self._request_timeout = request_timeout

    def _url(self, path: str) -> str:
        return f"{self._scheme}://{self._host}{path}"

    async def probe(self, *, timeout: float = 5.0) -> OscCameraInfo:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as s:
            async with s.get(self._url("/osc/info")) as r:
                r.raise_for_status()
                data = await r.json()
        return OscCameraInfo(
            manufacturer=data.get("manufacturer", ""),
            model=data.get("model", ""),
            firmware_version=data.get("firmwareVersion", ""),
        )

    async def list_files(self) -> list[OscFileEntry]:
        body = {
            "name": "camera.listFiles",
            "parameters": {"fileType": "video", "entryCount": 100},
        }
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._request_timeout)
        ) as s:
            async with s.post(self._url("/osc/commands/execute"), json=body) as r:
                r.raise_for_status()
                data = await r.json()
        entries = data.get("results", {}).get("entries", [])
        return [
            OscFileEntry(
                name=e.get("name", ""),
                file_url=e.get("fileUrl", ""),
                size=int(e.get("size", 0)),
            )
            for e in entries
        ]

    async def download(
        self,
        *,
        remote_path: str,
        local_path: Path,
        expected_size: int | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        port_overrides: Iterable[int] | None = None,
    ) -> None:
        """Atomically download a file from the camera SD.

        Streams to ``local_path.with_suffix(local_path.suffix + '.part')``
        and renames on success. On any failure (network, size mismatch),
        deletes the partial file and raises :class:`OscDownloadError`.
        """
        partial = local_path.with_suffix(local_path.suffix + ".part")
        partial.parent.mkdir(parents=True, exist_ok=True)
        total = expected_size or 0
        ports = port_overrides if port_overrides is not None else FALLBACK_PORTS

        last_error: Exception | None = None
        for port in ports:
            url = f"{self._scheme}://{self._stripped_host()}:{port}{remote_path}"
            try:
                await self._stream_to_partial(url, partial, total, on_progress)
                size_on_disk = partial.stat().st_size
                if expected_size is not None and size_on_disk != expected_size:
                    raise OscDownloadError(
                        f"size mismatch: got {size_on_disk}, expected {expected_size}"
                    )
                os.replace(partial, local_path)
                return
            except (aiohttp.ClientError, OscDownloadError, asyncio.TimeoutError) as e:
                last_error = e
                if partial.exists():
                    partial.unlink(missing_ok=True)
                continue

        if partial.exists():
            partial.unlink(missing_ok=True)
        raise OscDownloadError(
            f"all download attempts failed for {remote_path}: {last_error}"
        )

    async def _stream_to_partial(
        self,
        url: str,
        partial: Path,
        expected_total: int,
        on_progress: Callable[[int, int], None] | None,
    ) -> None:
        timeout = aiohttp.ClientTimeout(
            total=None, sock_read=60.0, sock_connect=10.0
        )
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url) as r:
                r.raise_for_status()
                total = expected_total or int(
                    r.headers.get("Content-Length", "0") or 0
                )
                done = 0
                with partial.open("wb") as fh:
                    async for chunk in r.content.iter_chunked(PROGRESS_CHUNK):
                        fh.write(chunk)
                        done += len(chunk)
                        if on_progress is not None:
                            on_progress(done, total)

    def _stripped_host(self) -> str:
        if ":" in self._host:
            return self._host.split(":", 1)[0]
        return self._host
```

Note: the test passes `host=f"127.0.0.1:{port}"` (host:port combined). The `_stripped_host()` helper handles that for the fallback-port loop, but the `probe()` and `list_files()` calls use `self._host` directly so the test's host:port works. Verify both pathways behave correctly in the test.

- [ ] **Step 4: Run tests**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_osc_client.py -v
```
Expected: all PASS. If `pytest-aiohttp` is missing, install:

```
uv add --dev pytest-aiohttp
```

- [ ] **Step 5: Commit**

```
git add src/syncfield/adapters/insta360_go3s/wifi/osc_client.py tests/unit/adapters/insta360_go3s/test_osc_client.py pyproject.toml uv.lock
git commit -m "feat(go3s/wifi): add OSC HTTP client with atomic download"
```

---

### Task 6: Build `WifiSwitcher` ABC + macOS implementation

**Files:**
- Create: `src/syncfield/adapters/insta360_go3s/wifi/switcher.py`
- Test: `tests/unit/adapters/insta360_go3s/test_wifi_switcher.py`

- [ ] **Step 1: Write failing test for ABC + Mac impl with subprocess mocked**

Create `tests/unit/adapters/insta360_go3s/test_wifi_switcher.py`:

```python
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from syncfield.adapters.insta360_go3s.wifi.switcher import (
    LinuxWifiSwitcher,
    MacWifiSwitcher,
    WifiSwitcher,
    WifiSwitcherError,
    WindowsWifiSwitcher,
    wifi_switcher_for_platform,
)


def test_abc_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        WifiSwitcher()  # type: ignore[abstract]


# ----- macOS -----

@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_mac_current_ssid_parses_networksetup_output(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="Current Wi-Fi Network: LabWiFi\n",
        stderr="",
    )
    sw = MacWifiSwitcher(interface="en0")
    assert sw.current_ssid() == "LabWiFi"


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_mac_current_ssid_returns_none_when_disconnected(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="You are not associated with an AirPort network.\n",
        stderr="",
    )
    sw = MacWifiSwitcher(interface="en0")
    assert sw.current_ssid() is None


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_mac_connect_invokes_setairportnetwork(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )
    sw = MacWifiSwitcher(interface="en0")
    sw.connect("Go3S-CAFEBABE.OSC", "88888888")
    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "networksetup"
    assert "-setairportnetwork" in cmd
    assert "en0" in cmd
    assert "Go3S-CAFEBABE.OSC" in cmd
    assert "88888888" in cmd


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_mac_connect_failure_raises(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="Could not find network"
    )
    sw = MacWifiSwitcher(interface="en0")
    with pytest.raises(WifiSwitcherError):
        sw.connect("does-not-exist", "x")


# ----- Linux -----

@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_linux_current_ssid_parses_iwgetid(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="LabWiFi\n", stderr=""
    )
    sw = LinuxWifiSwitcher(interface="wlan0")
    assert sw.current_ssid() == "LabWiFi"


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.subprocess.run")
def test_linux_connect_invokes_nmcli(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )
    sw = LinuxWifiSwitcher(interface="wlan0")
    sw.connect("Go3S-CAFEBABE.OSC", "88888888")
    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "nmcli"
    assert "wlan0" in cmd
    assert "Go3S-CAFEBABE.OSC" in cmd
    assert "88888888" in cmd


# ----- Windows stub -----

def test_windows_raises_not_implemented():
    sw = WindowsWifiSwitcher(interface="Wi-Fi")
    with pytest.raises(NotImplementedError):
        sw.connect("x", "y")


# ----- Factory -----

@patch("syncfield.adapters.insta360_go3s.wifi.switcher.sys.platform", "darwin")
def test_factory_returns_mac_on_darwin():
    sw = wifi_switcher_for_platform()
    assert isinstance(sw, MacWifiSwitcher)


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.sys.platform", "linux")
def test_factory_returns_linux_on_linux():
    sw = wifi_switcher_for_platform()
    assert isinstance(sw, LinuxWifiSwitcher)


@patch("syncfield.adapters.insta360_go3s.wifi.switcher.sys.platform", "win32")
def test_factory_returns_windows_on_win32():
    sw = wifi_switcher_for_platform()
    assert isinstance(sw, WindowsWifiSwitcher)
```

- [ ] **Step 2: Run to confirm failure**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_wifi_switcher.py -v
```
Expected: ImportError.

- [ ] **Step 3: Implement the switcher module**

Create `src/syncfield/adapters/insta360_go3s/wifi/switcher.py`:

```python
"""Cross-platform WiFi network switching for Insta360 Go3S aggregation.

Each :class:`WifiSwitcher` subclass owns one OS-native CLI for switching
the host's primary WiFi interface between the user's lab network and
the camera's AP. The factory :func:`wifi_switcher_for_platform` returns
the right subclass based on ``sys.platform``.
"""
from __future__ import annotations

import abc
import shutil
import subprocess
import sys
from typing import Optional


class WifiSwitcherError(RuntimeError):
    """Raised when a WiFi switch / restore step cannot be completed."""


class WifiSwitcher(abc.ABC):
    def __init__(self, *, interface: str):
        self.interface = interface

    @abc.abstractmethod
    def current_ssid(self) -> Optional[str]: ...

    @abc.abstractmethod
    def connect(self, ssid: str, password: str) -> None: ...

    def restore(self, prev_ssid: Optional[str], prev_password: Optional[str] = None) -> None:
        """Default restore: reconnect to ``prev_ssid`` if it's known.

        ``prev_password`` is rarely required (the OS keychain usually
        remembers it) but supported for completeness.
        """
        if prev_ssid is None:
            return
        self.connect(prev_ssid, prev_password or "")


# ----- macOS -----

class MacWifiSwitcher(WifiSwitcher):
    def current_ssid(self) -> Optional[str]:
        result = subprocess.run(
            ["networksetup", "-getairportnetwork", self.interface],
            capture_output=True,
            text=True,
            check=False,
        )
        line = (result.stdout or "").strip()
        prefix = "Current Wi-Fi Network: "
        if line.startswith(prefix):
            return line[len(prefix):].strip() or None
        return None

    def connect(self, ssid: str, password: str) -> None:
        result = subprocess.run(
            ["networksetup", "-setairportnetwork", self.interface, ssid, password],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or "Could not" in (result.stdout or ""):
            raise WifiSwitcherError(
                f"networksetup failed: rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
            )


# ----- Linux -----

class LinuxWifiSwitcher(WifiSwitcher):
    def current_ssid(self) -> Optional[str]:
        # Prefer iwgetid which is universally available; nmcli works too.
        if shutil.which("iwgetid"):
            r = subprocess.run(
                ["iwgetid", self.interface, "--raw"],
                capture_output=True,
                text=True,
                check=False,
            )
            ssid = (r.stdout or "").strip()
            return ssid or None
        # Fallback to nmcli
        r = subprocess.run(
            ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in (r.stdout or "").splitlines():
            if line.startswith("yes:"):
                return line.split(":", 1)[1] or None
        return None

    def connect(self, ssid: str, password: str) -> None:
        result = subprocess.run(
            [
                "nmcli", "device", "wifi", "connect", ssid,
                "password", password,
                "ifname", self.interface,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise WifiSwitcherError(
                f"nmcli failed: rc={result.returncode} stderr={result.stderr!r}"
            )


# ----- Windows (stub) -----

class WindowsWifiSwitcher(WifiSwitcher):
    def current_ssid(self) -> Optional[str]:
        raise NotImplementedError(
            "Windows WiFi switching is not supported in v1; "
            "use BLE-only mode or run on macOS/Linux."
        )

    def connect(self, ssid: str, password: str) -> None:
        raise NotImplementedError(
            "Windows WiFi switching is not supported in v1; "
            "use BLE-only mode or run on macOS/Linux."
        )


# ----- Factory -----

def wifi_switcher_for_platform(*, interface: Optional[str] = None) -> WifiSwitcher:
    if sys.platform == "darwin":
        return MacWifiSwitcher(interface=interface or "en0")
    if sys.platform.startswith("linux"):
        return LinuxWifiSwitcher(interface=interface or "wlan0")
    if sys.platform.startswith("win"):
        return WindowsWifiSwitcher(interface=interface or "Wi-Fi")
    raise WifiSwitcherError(f"Unsupported platform: {sys.platform}")
```

- [ ] **Step 4: Run tests**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_wifi_switcher.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/syncfield/adapters/insta360_go3s/wifi/switcher.py tests/unit/adapters/insta360_go3s/test_wifi_switcher.py
git commit -m "feat(go3s/wifi): add cross-platform WiFi switcher"
```

---

## Phase B — Aggregation Engine

### Task 7: Aggregation types

**Files:**
- Create: `src/syncfield/adapters/insta360_go3s/aggregation/types.py`
- Test: `tests/unit/adapters/insta360_go3s/test_aggregation_types.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/adapters/insta360_go3s/test_aggregation_types.py`:

```python
from pathlib import Path

from syncfield.adapters.insta360_go3s.aggregation.types import (
    AggregationCameraSpec,
    AggregationJob,
    AggregationProgress,
    AggregationState,
)


def test_state_values():
    assert AggregationState.PENDING.value == "pending"
    assert AggregationState.RUNNING.value == "running"
    assert AggregationState.COMPLETED.value == "completed"
    assert AggregationState.FAILED.value == "failed"


def test_camera_spec_round_trips_dict():
    spec = AggregationCameraSpec(
        stream_id="overhead",
        ble_address="AA:BB",
        wifi_ssid="Go3S-CAFEBABE.OSC",
        wifi_password="88888888",
        sd_path="/DCIM/Camera01/VID_FAKE.mp4",
        local_filename="overhead.mp4",
        size_bytes=12,
        done=False,
    )
    d = spec.to_dict()
    restored = AggregationCameraSpec.from_dict(d)
    assert restored == spec


def test_job_to_dict_includes_all_cameras(tmp_path):
    job = AggregationJob(
        job_id="agg_x",
        episode_id="ep_x",
        episode_dir=tmp_path,
        cameras=[
            AggregationCameraSpec(
                stream_id="overhead",
                ble_address="AA:BB",
                wifi_ssid="Go3S-X.OSC",
                wifi_password="88888888",
                sd_path="/DCIM/Camera01/VID.mp4",
                local_filename="overhead.mp4",
                size_bytes=0,
                done=False,
            )
        ],
        state=AggregationState.PENDING,
    )
    d = job.to_dict()
    assert d["job_id"] == "agg_x"
    assert d["episode_id"] == "ep_x"
    assert len(d["cameras"]) == 1
    assert d["state"] == "pending"


def test_progress_dataclass_defaults():
    p = AggregationProgress(
        job_id="agg_x",
        episode_id="ep_x",
        state=AggregationState.RUNNING,
        cameras_total=2,
        cameras_done=0,
    )
    assert p.current_stream_id is None
    assert p.current_bytes == 0
    assert p.current_total_bytes == 0
    assert p.error is None
```

- [ ] **Step 2: Run to confirm failure**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_aggregation_types.py -v
```
Expected: ImportError.

- [ ] **Step 3: Implement types**

Create `src/syncfield/adapters/insta360_go3s/aggregation/types.py`:

```python
"""Data types for the Insta360 Go3S aggregation queue."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class AggregationState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AggregationCameraSpec:
    stream_id: str
    ble_address: str
    wifi_ssid: str
    wifi_password: str
    sd_path: str
    local_filename: str
    size_bytes: int
    done: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AggregationCameraSpec":
        return cls(**data)


@dataclass
class AggregationJob:
    job_id: str
    episode_id: str
    episode_dir: Path
    cameras: list[AggregationCameraSpec]
    state: AggregationState = AggregationState.PENDING
    started_at_ns: Optional[int] = None
    completed_at_ns: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "episode_id": self.episode_id,
            "episode_dir": str(self.episode_dir),
            "cameras": [c.to_dict() for c in self.cameras],
            "state": self.state.value,
            "started_at_ns": self.started_at_ns,
            "completed_at_ns": self.completed_at_ns,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AggregationJob":
        return cls(
            job_id=data["job_id"],
            episode_id=data["episode_id"],
            episode_dir=Path(data["episode_dir"]),
            cameras=[AggregationCameraSpec.from_dict(c) for c in data["cameras"]],
            state=AggregationState(data["state"]),
            started_at_ns=data.get("started_at_ns"),
            completed_at_ns=data.get("completed_at_ns"),
            error=data.get("error"),
        )

    def manifest_path(self) -> Path:
        return self.episode_dir / "aggregation.json"

    def write_manifest(self) -> None:
        self.manifest_path().parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path().write_text(json.dumps(self.to_dict(), indent=2))


@dataclass
class AggregationProgress:
    job_id: str
    episode_id: str
    state: AggregationState
    cameras_total: int
    cameras_done: int
    current_stream_id: Optional[str] = None
    current_bytes: int = 0
    current_total_bytes: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "episode_id": self.episode_id,
            "state": self.state.value,
            "cameras_total": self.cameras_total,
            "cameras_done": self.cameras_done,
            "current_stream_id": self.current_stream_id,
            "current_bytes": self.current_bytes,
            "current_total_bytes": self.current_total_bytes,
            "error": self.error,
        }
```

- [ ] **Step 4: Run tests**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_aggregation_types.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/syncfield/adapters/insta360_go3s/aggregation/types.py tests/unit/adapters/insta360_go3s/test_aggregation_types.py
git commit -m "feat(go3s/aggregation): add job + progress types"
```

---

### Task 8: `AggregationQueue` worker

**Files:**
- Create: `src/syncfield/adapters/insta360_go3s/aggregation/queue.py`
- Test: `tests/unit/adapters/insta360_go3s/test_aggregation_queue.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/adapters/insta360_go3s/test_aggregation_queue.py`:

```python
import asyncio
from pathlib import Path
from typing import Any

import pytest

from syncfield.adapters.insta360_go3s.aggregation.queue import (
    AggregationDownloader,
    AggregationQueue,
)
from syncfield.adapters.insta360_go3s.aggregation.types import (
    AggregationCameraSpec,
    AggregationJob,
    AggregationProgress,
    AggregationState,
)


class FakeDownloader(AggregationDownloader):
    """Test double that simulates WiFi switch + OSC download."""

    def __init__(self, *, fail_on: set[str] | None = None):
        self.fail_on = fail_on or set()
        self.actions: list[str] = []

    async def run(self, camera: AggregationCameraSpec, target_dir: Path,
                  on_chunk: Any) -> None:
        self.actions.append(f"download:{camera.stream_id}")
        if camera.stream_id in self.fail_on:
            raise RuntimeError(f"injected failure for {camera.stream_id}")
        # Simulate two progress chunks then a completed file
        on_chunk(camera.stream_id, 6, camera.size_bytes)
        on_chunk(camera.stream_id, 12, camera.size_bytes)
        target = target_dir / camera.local_filename
        target.write_bytes(b"x" * 12)


def _make_job(tmp_path: Path, *stream_ids: str, size: int = 12) -> AggregationJob:
    return AggregationJob(
        job_id=f"job_{'_'.join(stream_ids)}",
        episode_id="ep_x",
        episode_dir=tmp_path,
        cameras=[
            AggregationCameraSpec(
                stream_id=sid,
                ble_address=f"AA:{sid}",
                wifi_ssid=f"Go3S-{sid}.OSC",
                wifi_password="88888888",
                sd_path=f"/DCIM/Camera01/{sid}.mp4",
                local_filename=f"{sid}.mp4",
                size_bytes=size,
            )
            for sid in stream_ids
        ],
    )


@pytest.mark.asyncio
async def test_enqueue_runs_to_completion(tmp_path):
    downloader = FakeDownloader()
    progress_log: list[AggregationProgress] = []
    q = AggregationQueue(downloader=downloader)
    q.subscribe(lambda p: progress_log.append(p))
    await q.start()

    job = _make_job(tmp_path, "cam_a", "cam_b")
    handle = q.enqueue(job)
    final = await handle.wait()

    assert final.state == AggregationState.COMPLETED
    assert final.cameras_done == 2
    assert (tmp_path / "cam_a.mp4").exists()
    assert (tmp_path / "cam_b.mp4").exists()
    assert any(p.state == AggregationState.RUNNING for p in progress_log)
    assert progress_log[-1].state == AggregationState.COMPLETED
    await q.shutdown()


@pytest.mark.asyncio
async def test_failure_marks_job_failed_and_preserves_other_files(tmp_path):
    downloader = FakeDownloader(fail_on={"cam_b"})
    q = AggregationQueue(downloader=downloader)
    await q.start()
    job = _make_job(tmp_path, "cam_a", "cam_b")
    handle = q.enqueue(job)
    final = await handle.wait()
    assert final.state == AggregationState.FAILED
    assert "cam_b" in (final.error or "")
    # cam_a should have completed
    assert (tmp_path / "cam_a.mp4").exists()
    await q.shutdown()


@pytest.mark.asyncio
async def test_retry_re_runs_only_failed_cameras(tmp_path):
    downloader = FakeDownloader(fail_on={"cam_b"})
    q = AggregationQueue(downloader=downloader)
    await q.start()
    job = _make_job(tmp_path, "cam_a", "cam_b")
    handle = q.enqueue(job)
    await handle.wait()

    # Heal the downloader and retry
    downloader.fail_on = set()
    downloader.actions.clear()
    handle2 = q.retry(job.job_id)
    final = await handle2.wait()
    assert final.state == AggregationState.COMPLETED
    # Only cam_b should have been re-downloaded
    assert downloader.actions == ["download:cam_b"]
    await q.shutdown()


@pytest.mark.asyncio
async def test_recover_pending_jobs_from_disk(tmp_path):
    job = _make_job(tmp_path, "cam_a")
    job.write_manifest()

    downloader = FakeDownloader()
    q = AggregationQueue(downloader=downloader)
    recovered = q.recover_from_disk(search_root=tmp_path.parent)
    assert any(j.job_id == job.job_id for j in recovered)
    await q.start()
    handle = q.enqueue(recovered[0])
    final = await handle.wait()
    assert final.state == AggregationState.COMPLETED
    await q.shutdown()
```

- [ ] **Step 2: Run to confirm failure**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_aggregation_queue.py -v
```
Expected: ImportError.

- [ ] **Step 3: Implement the queue**

Create `src/syncfield/adapters/insta360_go3s/aggregation/queue.py`:

```python
"""Background aggregation queue for Insta360 Go3S episodes.

A single asyncio worker processes :class:`AggregationJob`s in FIFO order.
Per-camera atomicity: a failed download leaves no partial files for that
camera. Per-episode atomicity: a job is COMPLETED only when every camera
succeeds; otherwise FAILED with per-camera breakdown for selective retry.
"""
from __future__ import annotations

import abc
import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from .types import (
    AggregationCameraSpec,
    AggregationJob,
    AggregationProgress,
    AggregationState,
)

ProgressListener = Callable[[AggregationProgress], None]
ChunkCallback = Callable[[str, int, int], None]  # (stream_id, done, total)


class AggregationDownloader(abc.ABC):
    """Pluggable backend that performs the WiFi switch + OSC download for one camera."""

    @abc.abstractmethod
    async def run(
        self,
        camera: AggregationCameraSpec,
        target_dir: Path,
        on_chunk: ChunkCallback,
    ) -> None: ...


@dataclass
class _JobHandle:
    job: AggregationJob
    done: asyncio.Event
    final_progress: Optional[AggregationProgress] = None

    async def wait(self) -> AggregationProgress:
        await self.done.wait()
        assert self.final_progress is not None
        return self.final_progress


class AggregationQueue:
    def __init__(self, *, downloader: AggregationDownloader):
        self._downloader = downloader
        self._queue: asyncio.Queue[_JobHandle] = asyncio.Queue()
        self._handles: dict[str, _JobHandle] = {}
        self._listeners: list[ProgressListener] = []
        self._worker_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ----- public API -----

    async def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._stop.clear()
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def shutdown(self) -> None:
        self._stop.set()
        # poison-pill: enqueue a sentinel
        await self._queue.put(None)  # type: ignore[arg-type]
        if self._worker_task is not None:
            await self._worker_task
            self._worker_task = None

    def subscribe(self, listener: ProgressListener) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: ProgressListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def enqueue(self, job: AggregationJob) -> _JobHandle:
        handle = _JobHandle(job=job, done=asyncio.Event())
        self._handles[job.job_id] = handle
        job.write_manifest()
        self._queue.put_nowait(handle)
        return handle

    def retry(self, job_id: str) -> _JobHandle:
        handle = self._handles.get(job_id)
        if handle is None:
            raise KeyError(job_id)
        # Reset state but keep already-done cameras
        handle.job.state = AggregationState.PENDING
        handle.job.error = None
        handle.done = asyncio.Event()
        handle.final_progress = None
        handle.job.write_manifest()
        self._queue.put_nowait(handle)
        return handle

    def status(self, job_id: str) -> Optional[AggregationProgress]:
        handle = self._handles.get(job_id)
        return handle.final_progress if handle else None

    def recover_from_disk(self, *, search_root: Path) -> list[AggregationJob]:
        """Find aggregation.json files under search_root and return jobs that were unfinished."""
        recovered: list[AggregationJob] = []
        for manifest in search_root.rglob("aggregation.json"):
            try:
                data = json.loads(manifest.read_text())
                job = AggregationJob.from_dict(data)
            except Exception:
                continue
            if job.state in (AggregationState.PENDING, AggregationState.RUNNING):
                # Reset RUNNING -> PENDING on recovery
                job.state = AggregationState.PENDING
                recovered.append(job)
        return recovered

    # ----- worker -----

    async def _worker_loop(self) -> None:
        while not self._stop.is_set():
            handle = await self._queue.get()
            if handle is None:
                break
            try:
                await self._run_job(handle)
            except Exception as e:  # last-resort guard so worker keeps running
                handle.job.state = AggregationState.FAILED
                handle.job.error = f"worker crash: {e}"
                handle.job.write_manifest()
                final = self._snapshot(handle.job)
                handle.final_progress = final
                self._notify(final)
                handle.done.set()

    async def _run_job(self, handle: _JobHandle) -> None:
        job = handle.job
        job.state = AggregationState.RUNNING
        job.started_at_ns = time.monotonic_ns()
        job.error = None
        job.write_manifest()
        self._notify(self._snapshot(job))

        any_failure = False
        for camera in job.cameras:
            if camera.done:
                continue
            current = AggregationProgress(
                job_id=job.job_id,
                episode_id=job.episode_id,
                state=AggregationState.RUNNING,
                cameras_total=len(job.cameras),
                cameras_done=sum(1 for c in job.cameras if c.done),
                current_stream_id=camera.stream_id,
                current_bytes=0,
                current_total_bytes=camera.size_bytes,
            )
            self._notify(current)

            def chunk_cb(stream_id: str, done: int, total: int, *, _cam=camera) -> None:
                p = AggregationProgress(
                    job_id=job.job_id,
                    episode_id=job.episode_id,
                    state=AggregationState.RUNNING,
                    cameras_total=len(job.cameras),
                    cameras_done=sum(1 for c in job.cameras if c.done),
                    current_stream_id=_cam.stream_id,
                    current_bytes=done,
                    current_total_bytes=total,
                )
                self._notify(p)

            try:
                await self._downloader.run(camera, job.episode_dir, chunk_cb)
                camera.done = True
                camera.error = None
            except Exception as e:
                camera.done = False
                camera.error = str(e)
                any_failure = True
            job.write_manifest()

        job.completed_at_ns = time.monotonic_ns()
        if any_failure:
            job.state = AggregationState.FAILED
            failed_ids = [c.stream_id for c in job.cameras if not c.done]
            job.error = f"failed cameras: {failed_ids}"
        else:
            job.state = AggregationState.COMPLETED
            job.error = None
        job.write_manifest()

        final = self._snapshot(job)
        handle.final_progress = final
        self._notify(final)
        handle.done.set()

    def _snapshot(self, job: AggregationJob) -> AggregationProgress:
        return AggregationProgress(
            job_id=job.job_id,
            episode_id=job.episode_id,
            state=job.state,
            cameras_total=len(job.cameras),
            cameras_done=sum(1 for c in job.cameras if c.done),
            current_stream_id=None,
            current_bytes=0,
            current_total_bytes=0,
            error=job.error,
        )

    def _notify(self, progress: AggregationProgress) -> None:
        for listener in list(self._listeners):
            try:
                listener(progress)
            except Exception:
                # Do not let a buggy listener take down the worker
                pass


def make_job_id() -> str:
    return f"agg_{uuid.uuid4().hex[:12]}"
```

- [ ] **Step 4: Run tests**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_aggregation_queue.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/syncfield/adapters/insta360_go3s/aggregation/queue.py tests/unit/adapters/insta360_go3s/test_aggregation_queue.py
git commit -m "feat(go3s/aggregation): add background queue worker with retry"
```

---

### Task 9: Production `AggregationDownloader` wiring WiFi + OSC

**Files:**
- Modify: `src/syncfield/adapters/insta360_go3s/aggregation/queue.py` (add `Go3SAggregationDownloader`)
- Test: extend `tests/unit/adapters/insta360_go3s/test_aggregation_queue.py`

- [ ] **Step 1: Write failing test for Go3SAggregationDownloader's switch+probe+download flow**

Append to `tests/unit/adapters/insta360_go3s/test_aggregation_queue.py`:

```python
class FakeSwitcher:
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []
        self._current: str | None = "LabWiFi"

    def current_ssid(self) -> str | None:
        return self._current

    def connect(self, ssid: str, password: str) -> None:
        self.calls.append(("connect", ssid))
        self._current = ssid

    def restore(self, prev_ssid: str | None, prev_password: str | None = None) -> None:
        self.calls.append(("restore", prev_ssid))
        self._current = prev_ssid


class FakeOscClient:
    def __init__(self, *, fail_probe: bool = False, fail_download: bool = False):
        self.fail_probe = fail_probe
        self.fail_download = fail_download
        self.downloads: list[tuple[str, Path]] = []

    async def probe(self, *, timeout: float = 5.0):
        if self.fail_probe:
            raise RuntimeError("probe failed")
        from syncfield.adapters.insta360_go3s.wifi.osc_client import OscCameraInfo
        return OscCameraInfo(manufacturer="Insta360", model="Go 3S", firmware_version="x")

    async def download(self, *, remote_path: str, local_path: Path,
                       expected_size: int | None = None, on_progress=None,
                       port_overrides=None) -> None:
        self.downloads.append((remote_path, local_path))
        if self.fail_download:
            raise RuntimeError("download failed")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"x" * (expected_size or 0))
        if on_progress:
            on_progress(expected_size or 0, expected_size or 0)


@pytest.mark.asyncio
async def test_production_downloader_switches_downloads_restores(tmp_path, monkeypatch):
    from syncfield.adapters.insta360_go3s.aggregation.queue import (
        Go3SAggregationDownloader,
    )

    sw = FakeSwitcher()
    osc = FakeOscClient()

    def osc_factory(host: str):
        return osc

    downloader = Go3SAggregationDownloader(
        switcher=sw,
        osc_factory=osc_factory,
        wait_for_ap_timeout=0.1,
        ap_probe_attempts=1,
    )
    cam = AggregationCameraSpec(
        stream_id="overhead",
        ble_address="AA:BB",
        wifi_ssid="Go3S-CAFEBABE.OSC",
        wifi_password="88888888",
        sd_path="/DCIM/Camera01/VID.mp4",
        local_filename="overhead.mp4",
        size_bytes=12,
    )
    progress: list[tuple[str, int, int]] = []
    await downloader.run(cam, tmp_path, lambda sid, d, t: progress.append((sid, d, t)))

    assert sw.calls == [("connect", "Go3S-CAFEBABE.OSC"), ("restore", "LabWiFi")]
    assert (tmp_path / "overhead.mp4").exists()
    assert progress[-1] == ("overhead", 12, 12)


@pytest.mark.asyncio
async def test_production_downloader_restores_wifi_even_on_failure(tmp_path):
    from syncfield.adapters.insta360_go3s.aggregation.queue import (
        Go3SAggregationDownloader,
    )

    sw = FakeSwitcher()
    osc = FakeOscClient(fail_download=True)

    downloader = Go3SAggregationDownloader(
        switcher=sw,
        osc_factory=lambda host: osc,
        wait_for_ap_timeout=0.1,
        ap_probe_attempts=1,
    )
    cam = AggregationCameraSpec(
        stream_id="overhead",
        ble_address="AA:BB",
        wifi_ssid="Go3S-CAFEBABE.OSC",
        wifi_password="88888888",
        sd_path="/DCIM/Camera01/VID.mp4",
        local_filename="overhead.mp4",
        size_bytes=12,
    )
    with pytest.raises(RuntimeError):
        await downloader.run(cam, tmp_path, lambda *args: None)
    # Restore must still be called
    assert ("restore", "LabWiFi") in sw.calls
```

- [ ] **Step 2: Run to confirm failure**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_aggregation_queue.py -v
```
Expected: ImportError on `Go3SAggregationDownloader`.

- [ ] **Step 3: Implement `Go3SAggregationDownloader`**

Append to `src/syncfield/adapters/insta360_go3s/aggregation/queue.py`:

```python
from .types import AggregationCameraSpec  # noqa: E402  (kept for clarity)

# At top, also add:
# from ..wifi.osc_client import OscHttpClient, OscDownloadError
# from ..wifi.switcher import WifiSwitcher

class Go3SAggregationDownloader(AggregationDownloader):
    """Production downloader: switch WiFi -> probe OSC -> download -> restore."""

    def __init__(
        self,
        *,
        switcher,                              # WifiSwitcher
        osc_factory: Callable[[str], object],  # (host) -> OscHttpClient-like
        ap_host: str = "192.168.42.1",
        wait_for_ap_timeout: float = 30.0,
        ap_probe_attempts: int = 6,
        ap_probe_interval: float = 5.0,
    ):
        self._switcher = switcher
        self._osc_factory = osc_factory
        self._ap_host = ap_host
        self._wait_for_ap_timeout = wait_for_ap_timeout
        self._ap_probe_attempts = ap_probe_attempts
        self._ap_probe_interval = ap_probe_interval

    async def run(
        self,
        camera: AggregationCameraSpec,
        target_dir: Path,
        on_chunk: ChunkCallback,
    ) -> None:
        prev_ssid = self._switcher.current_ssid()
        try:
            self._switcher.connect(camera.wifi_ssid, camera.wifi_password)
            await self._wait_for_ap()
            osc = self._osc_factory(self._ap_host)
            await osc.probe(timeout=5.0)
            local_path = target_dir / camera.local_filename
            await osc.download(
                remote_path=camera.sd_path,
                local_path=local_path,
                expected_size=camera.size_bytes or None,
                on_progress=lambda done, total: on_chunk(camera.stream_id, done, total),
            )
        finally:
            try:
                self._switcher.restore(prev_ssid)
            except Exception:
                # Restore best-effort; surface via health event upstream
                pass

    async def _wait_for_ap(self) -> None:
        deadline = asyncio.get_event_loop().time() + self._wait_for_ap_timeout
        last_error: Exception | None = None
        for attempt in range(self._ap_probe_attempts):
            if asyncio.get_event_loop().time() > deadline:
                break
            try:
                osc = self._osc_factory(self._ap_host)
                await osc.probe(timeout=2.0)
                return
            except Exception as e:
                last_error = e
                await asyncio.sleep(self._ap_probe_interval)
        raise RuntimeError(f"camera AP unreachable: {last_error}")
```

- [ ] **Step 4: Run tests**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_aggregation_queue.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add src/syncfield/adapters/insta360_go3s/aggregation/queue.py tests/unit/adapters/insta360_go3s/test_aggregation_queue.py
git commit -m "feat(go3s/aggregation): production downloader with WiFi + OSC"
```

---

## Phase C — Stream Class & Orchestrator

### Task 10: `Go3SStream` lifecycle

**Files:**
- Create: `src/syncfield/adapters/insta360_go3s/stream.py`
- Create: `src/syncfield/adapters/insta360_go3s/__init__.py` (already exists, extend exports)
- Test: `tests/unit/adapters/insta360_go3s/test_go3s_stream.py`

- [ ] **Step 1: Write failing test for Stream lifecycle with all backends faked**

Create `tests/unit/adapters/insta360_go3s/test_go3s_stream.py`:

```python
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from syncfield.adapters.insta360_go3s import Go3SStream
from syncfield.adapters.insta360_go3s.aggregation.queue import AggregationQueue
from syncfield.adapters.insta360_go3s.aggregation.types import AggregationState
from syncfield.adapters.insta360_go3s.ble.camera import CaptureResult
from syncfield.types import StreamCapabilities


@pytest.fixture
def fake_ble(monkeypatch):
    """Replace Go3SBLECamera with an async-mock."""
    fake = AsyncMock()
    fake.connect = AsyncMock()
    fake.disconnect = AsyncMock()
    fake.set_video_mode = AsyncMock()
    fake.start_capture = AsyncMock(return_value=12345)  # fake host_ns
    fake.stop_capture = AsyncMock(
        return_value=CaptureResult(
            file_path="/DCIM/Camera01/VID_FAKE.mp4", ack_host_ns=23456
        )
    )

    def factory(address):
        return fake

    monkeypatch.setattr(
        "syncfield.adapters.insta360_go3s.stream.Go3SBLECamera", factory
    )
    return fake


@pytest.fixture
def fake_queue(monkeypatch):
    queue = MagicMock(spec=AggregationQueue)
    queue.enqueue = MagicMock()
    monkeypatch.setattr(
        "syncfield.adapters.insta360_go3s.stream._global_aggregation_queue",
        lambda: queue,
    )
    return queue


def test_capabilities_indicate_no_live_preview_and_produces_file(fake_ble, tmp_path):
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
    )
    caps: StreamCapabilities = s.capabilities
    assert caps.live_preview is False
    assert caps.produces_file is True
    assert caps.is_removable is True


def test_device_key_is_go3s_with_address(fake_ble, tmp_path):
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
    )
    assert s.device_key == ("go3s", "AA:BB:CC:DD:EE:FF")


def test_kind_is_video(fake_ble, tmp_path):
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
    )
    assert s.kind == "video"


@pytest.mark.asyncio
async def test_full_lifecycle_enqueues_aggregation(fake_ble, fake_queue, tmp_path):
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
    )
    s.prepare()
    s.connect()
    # start_recording is a sync API; wrap async work internally.
    s.start_recording(session_clock=MagicMock())
    report = s.stop_recording()
    s.disconnect()

    assert report.status == "pending_aggregation"
    assert report.stream_id == "overhead"
    fake_queue.enqueue.assert_called_once()
    enq_job = fake_queue.enqueue.call_args.args[0]
    assert enq_job.cameras[0].stream_id == "overhead"
    assert enq_job.cameras[0].sd_path == "/DCIM/Camera01/VID_FAKE.mp4"
    assert enq_job.cameras[0].local_filename == "overhead.mp4"


def test_on_demand_policy_does_not_enqueue(fake_ble, fake_queue, tmp_path):
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
        aggregation_policy="on_demand",
    )
    s.prepare()
    s.connect()
    s.start_recording(session_clock=MagicMock())
    report = s.stop_recording()
    assert report.status == "pending_aggregation"
    assert not fake_queue.enqueue.called
    # An ID for manual aggregation later should still be exposed
    assert s.pending_aggregation_job is not None
    assert s.pending_aggregation_job.cameras[0].sd_path == "/DCIM/Camera01/VID_FAKE.mp4"
```

- [ ] **Step 2: Run to confirm failure**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_go3s_stream.py -v
```
Expected: ImportError.

- [ ] **Step 3: Implement `Go3SStream`**

Create `src/syncfield/adapters/insta360_go3s/stream.py`:

```python
"""Insta360 Go3S Stream — BLE trigger + deferred WiFi aggregation."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

from syncfield.clock import SessionClock
from syncfield.stream import StreamBase
from syncfield.types import (
    DeviceKey,
    FinalizationReport,
    HealthEvent,
    StreamCapabilities,
    StreamKind,
)

from .aggregation.queue import (
    AggregationQueue,
    Go3SAggregationDownloader,
    make_job_id,
)
from .aggregation.types import (
    AggregationCameraSpec,
    AggregationJob,
    AggregationState,
)
from .ble.camera import Go3SBLECamera
from .wifi.osc_client import OscHttpClient
from .wifi.switcher import wifi_switcher_for_platform


AggregationPolicy = Literal["eager", "on_demand", "between_sessions"]


_QUEUE_LOCK = threading.Lock()
_QUEUE: Optional[AggregationQueue] = None


def _global_aggregation_queue() -> AggregationQueue:
    """Lazily construct (and start) the singleton aggregation queue.

    Uses the production downloader (real WiFi switcher + OSC client).
    Tests inject a fake by monkeypatching this function.
    """
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is not None:
            return _QUEUE
        switcher = wifi_switcher_for_platform()
        downloader = Go3SAggregationDownloader(
            switcher=switcher,
            osc_factory=lambda host: OscHttpClient(host=host),
        )
        _QUEUE = AggregationQueue(downloader=downloader)
        # Defer asyncio.start() to first enqueue from inside an event loop.
    return _QUEUE


class Go3SStream(StreamBase):
    """Insta360 Go3S adapter — wireless start/stop + background aggregation."""

    def __init__(
        self,
        stream_id: str,
        *,
        ble_address: str,
        output_dir: Path,
        aggregation_policy: AggregationPolicy = "eager",
        wifi_ssid: Optional[str] = None,
        wifi_password: str = "88888888",
    ):
        super().__init__(stream_id=stream_id)
        self._ble_address = ble_address
        self._output_dir = Path(output_dir)
        self._aggregation_policy: AggregationPolicy = aggregation_policy
        self._wifi_ssid = wifi_ssid  # auto-derived from BLE name on first connect if None
        self._wifi_password = wifi_password
        self._capabilities = StreamCapabilities(
            provides_audio_track=False,
            supports_precise_timestamps=False,
            is_removable=True,
            produces_file=True,
            live_preview=False,
        )
        self._cam: Optional[Go3SBLECamera] = None
        self._start_ack_ns: Optional[int] = None
        self._stop_ack_ns: Optional[int] = None
        self._sd_path: Optional[str] = None
        self.pending_aggregation_job: Optional[AggregationJob] = None

    # ----- Stream protocol -----

    @property
    def kind(self) -> StreamKind:
        return "video"

    @property
    def capabilities(self) -> StreamCapabilities:
        return self._capabilities

    @property
    def device_key(self) -> Optional[DeviceKey]:
        return ("go3s", self._ble_address)

    def prepare(self) -> None:
        # No-op: BLE/WiFi resources are owned by transient async helpers.
        self._emit_health(HealthEvent(
            stream_id=self.id,
            level="info",
            message="Go3S prepared",
        ))

    def connect(self) -> None:
        # Quick BLE handshake to verify reachability, then disconnect.
        self._run_async(self._verify_reachable())

    def start_recording(self, session_clock: SessionClock) -> None:
        self._run_async(self._do_start())

    def stop_recording(self) -> FinalizationReport:
        self._run_async(self._do_stop())
        job = self._build_job()
        self.pending_aggregation_job = job
        if self._aggregation_policy == "eager":
            self._enqueue_job(job)
        elif self._aggregation_policy == "between_sessions":
            self._enqueue_job(job)
        # "on_demand": leave job pending; orchestrator/viewer triggers later.
        return FinalizationReport(
            stream_id=self.id,
            status="pending_aggregation",
            frame_count=0,
            file_path=None,
            first_sample_at_ns=self._start_ack_ns,
            last_sample_at_ns=self._stop_ack_ns,
            health_events=list(self._collected_health),
            error=None,
        )

    def disconnect(self) -> None:
        # Aggregation runs independently; nothing to tear down synchronously.
        self._cam = None

    # ----- internals -----

    async def _verify_reachable(self) -> None:
        cam = Go3SBLECamera(self._ble_address)
        await cam.connect(sync_timeout=2.0, auth_timeout=1.0)
        # Auto-derive WiFi SSID from BLE device name if not supplied.
        if self._wifi_ssid is None:
            self._wifi_ssid = self._derive_ssid_from_address(self._ble_address)
        await cam.disconnect()

    async def _do_start(self) -> None:
        cam = Go3SBLECamera(self._ble_address)
        await cam.connect()
        try:
            self._start_ack_ns = await cam.start_capture()
        finally:
            await cam.disconnect()

    async def _do_stop(self) -> None:
        cam = Go3SBLECamera(self._ble_address)
        await cam.connect()
        try:
            result = await cam.stop_capture()
            self._stop_ack_ns = result.ack_host_ns
            self._sd_path = result.file_path
        finally:
            await cam.disconnect()

    def _build_job(self) -> AggregationJob:
        if self._sd_path is None:
            raise RuntimeError("stop_recording did not return a file path")
        ext = ".mp4" if self._sd_path.lower().endswith(".mp4") else ".insv"
        camera_spec = AggregationCameraSpec(
            stream_id=self.id,
            ble_address=self._ble_address,
            wifi_ssid=self._wifi_ssid or self._derive_ssid_from_address(self._ble_address),
            wifi_password=self._wifi_password,
            sd_path=self._sd_path,
            local_filename=f"{self.id}{ext}",
            size_bytes=0,  # populated by OSC listFiles in production downloader
        )
        return AggregationJob(
            job_id=make_job_id(),
            episode_id=self._output_dir.name,
            episode_dir=self._output_dir,
            cameras=[camera_spec],
            state=AggregationState.PENDING,
        )

    def _enqueue_job(self, job: AggregationJob) -> None:
        queue = _global_aggregation_queue()
        # Ensure the worker is running on this loop.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        if not loop.is_running():
            loop.run_until_complete(queue.start())
        else:
            asyncio.run_coroutine_threadsafe(queue.start(), loop)
        queue.enqueue(job)

    @staticmethod
    def _derive_ssid_from_address(address: str) -> str:
        suffix = address.replace(":", "").upper()[-12:]
        return f"Go3S-{suffix}.OSC"

    def _run_async(self, coro) -> None:
        """Bridge sync Stream API to the async BLE helper."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        if loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            fut.result(timeout=30.0)
        else:
            loop.run_until_complete(coro)
```

Edit `src/syncfield/adapters/insta360_go3s/__init__.py` to export the public API:

```python
"""Insta360 Go3S adapter (BLE trigger + WiFi aggregation)."""

from .stream import Go3SStream

__all__ = ["Go3SStream"]
```

- [ ] **Step 4: Run tests**

```
uv run pytest tests/unit/adapters/insta360_go3s/test_go3s_stream.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/syncfield/adapters/insta360_go3s/stream.py src/syncfield/adapters/insta360_go3s/__init__.py tests/unit/adapters/insta360_go3s/test_go3s_stream.py
git commit -m "feat(go3s): add Go3SStream with deferred aggregation"
```

---

### Task 11: Lazy re-export from `syncfield.adapters`

**Files:**
- Modify: `src/syncfield/adapters/__init__.py`

- [ ] **Step 1: Add lazy import for Go3SStream**

Read the existing `src/syncfield/adapters/__init__.py`. It uses lazy `__getattr__` for adapters with optional dependencies. Add a clause:

```python
def __getattr__(name: str):
    # ... existing clauses ...
    if name == "Go3SStream":
        try:
            from .insta360_go3s import Go3SStream
        except ImportError as e:
            raise ImportError(
                "Go3SStream requires the 'camera' optional dependency: "
                "`uv add 'syncfield[camera]'`"
            ) from e
        return Go3SStream
    raise AttributeError(name)
```

Also append `"Go3SStream"` to the module's `__all__` if one is defined.

- [ ] **Step 2: Smoke import**

```
uv run python -c "from syncfield.adapters import Go3SStream; print(Go3SStream.__name__)"
```
Expected: `Go3SStream`.

- [ ] **Step 3: Commit**

```
git add src/syncfield/adapters/__init__.py
git commit -m "feat(adapters): re-export Go3SStream lazily"
```

---

### Task 12: Orchestrator multihost auto-downgrade

**Files:**
- Modify: `src/syncfield/orchestrator.py` (the `add()` method)
- Test: `tests/unit/test_orchestrator_go3s_policy_downgrade.py` (new)

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_orchestrator_go3s_policy_downgrade.py`:

```python
from pathlib import Path
from unittest.mock import patch

import pytest

from syncfield.adapters.insta360_go3s import Go3SStream
from syncfield.orchestrator import SessionOrchestrator
from syncfield.roles import LeaderRole, FollowerRole


@patch("syncfield.adapters.insta360_go3s.stream.Go3SBLECamera")
def test_eager_downgrades_to_on_demand_when_leader(_mock_cam, tmp_path):
    session = SessionOrchestrator(
        host_id="mac",
        output_dir=tmp_path,
        role=LeaderRole(session_id="sess_x"),
    )
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
        aggregation_policy="eager",
    )
    session.add(s)
    assert s._aggregation_policy == "on_demand"


@patch("syncfield.adapters.insta360_go3s.stream.Go3SBLECamera")
def test_eager_downgrades_to_on_demand_when_follower(_mock_cam, tmp_path):
    session = SessionOrchestrator(
        host_id="mac",
        output_dir=tmp_path,
        role=FollowerRole(session_id="sess_x"),
    )
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
        aggregation_policy="eager",
    )
    session.add(s)
    assert s._aggregation_policy == "on_demand"


@patch("syncfield.adapters.insta360_go3s.stream.Go3SBLECamera")
def test_eager_unchanged_when_single_host(_mock_cam, tmp_path):
    session = SessionOrchestrator(host_id="mac", output_dir=tmp_path)
    s = Go3SStream(
        stream_id="overhead",
        ble_address="AA:BB:CC:DD:EE:FF",
        output_dir=tmp_path,
        aggregation_policy="eager",
    )
    session.add(s)
    assert s._aggregation_policy == "eager"
```

- [ ] **Step 2: Run to confirm failure**

```
uv run pytest tests/unit/test_orchestrator_go3s_policy_downgrade.py -v
```
Expected: at least one failure (likely on `LeaderRole` / `FollowerRole` constructor signature or downgrade absent).

- [ ] **Step 3: Locate `add()` in orchestrator**

Run:
```
grep -n "def add" src/syncfield/orchestrator.py
```
Identify the line number; we'll insert a Go3S-specific hook right before the stream is appended to the registry.

- [ ] **Step 4: Implement the downgrade in `add()`**

Edit `src/syncfield/orchestrator.py` `add()` method. Add at the start of the method body (after argument validation, before existing registration logic):

```python
# Multihost role-aware policy downgrade for Go3S streams to keep the
# leader/follower attached to lab WiFi (mDNS) during recording.
try:
    from syncfield.adapters.insta360_go3s import Go3SStream  # type: ignore
    if isinstance(stream, Go3SStream) and stream._aggregation_policy == "eager":
        if self._role is not None and not isinstance(self._role, type(None)):
            role_name = type(self._role).__name__
            if role_name in ("LeaderRole", "FollowerRole"):
                stream._aggregation_policy = "on_demand"
                # Emit health so users see why aggregation isn't running.
                # (Use whatever health-event API the orchestrator already exposes.)
except ImportError:
    pass
```

If the orchestrator stores its role under a different attribute (e.g. `self.role`), use that. If it doesn't store a role at all, locate where `LeaderRole`/`FollowerRole` is consumed (search for `LeaderRole`) and read the role from there.

- [ ] **Step 5: Run tests**

```
uv run pytest tests/unit/test_orchestrator_go3s_policy_downgrade.py -v
```
Expected: PASS. If `_role` doesn't exist, adjust attribute name and re-run.

- [ ] **Step 6: Commit**

```
git add src/syncfield/orchestrator.py tests/unit/test_orchestrator_go3s_policy_downgrade.py
git commit -m "feat(orchestrator): downgrade Go3S aggregation to on_demand for multihost roles"
```

---

## Phase D — Viewer Backend

### Task 13: Extend WS snapshot with aggregation state

**Files:**
- Modify: `src/syncfield/viewer/server.py` (around `snapshot_to_dict`)
- Modify: `src/syncfield/viewer/state.py` (if it owns the snapshot dataclass)
- Test: `tests/unit/test_viewer_aggregation_snapshot.py` (new)

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_viewer_aggregation_snapshot.py`:

```python
from unittest.mock import MagicMock

import pytest

from syncfield.adapters.insta360_go3s.aggregation.types import (
    AggregationProgress,
    AggregationState,
)
from syncfield.viewer.server import snapshot_to_dict


def test_snapshot_includes_aggregation_section_empty_by_default():
    snapshot = MagicMock()
    snapshot.aggregation = None
    d = snapshot_to_dict(snapshot)
    assert "aggregation" in d
    assert d["aggregation"]["active_job"] is None
    assert d["aggregation"]["queue_length"] == 0
    assert d["aggregation"]["recent_jobs"] == []


def test_snapshot_serializes_active_job():
    progress = AggregationProgress(
        job_id="agg_x",
        episode_id="ep_x",
        state=AggregationState.RUNNING,
        cameras_total=2,
        cameras_done=1,
        current_stream_id="overhead",
        current_bytes=5_000_000,
        current_total_bytes=10_000_000,
    )
    snapshot = MagicMock()
    snapshot.aggregation = MagicMock()
    snapshot.aggregation.active_job = progress
    snapshot.aggregation.queue_length = 1
    snapshot.aggregation.recent_jobs = [progress]
    d = snapshot_to_dict(snapshot)
    assert d["aggregation"]["active_job"]["state"] == "running"
    assert d["aggregation"]["active_job"]["current_bytes"] == 5_000_000
    assert d["aggregation"]["queue_length"] == 1
    assert len(d["aggregation"]["recent_jobs"]) == 1
```

- [ ] **Step 2: Run to confirm failure**

```
uv run pytest tests/unit/test_viewer_aggregation_snapshot.py -v
```
Expected: KeyError or AttributeError.

- [ ] **Step 3: Locate `snapshot_to_dict` and the snapshot dataclass**

Run:
```
grep -n "def snapshot_to_dict" src/syncfield/viewer/server.py
grep -rn "class.*Snapshot" src/syncfield/viewer/state.py src/syncfield/viewer/server.py
```

- [ ] **Step 4: Add `aggregation` field to snapshot dataclass**

Wherever the snapshot dataclass lives (likely `src/syncfield/viewer/state.py`):

```python
from typing import Optional
from syncfield.adapters.insta360_go3s.aggregation.types import AggregationProgress

@dataclass
class AggregationSnapshot:
    active_job: Optional[AggregationProgress] = None
    queue_length: int = 0
    recent_jobs: list[AggregationProgress] = field(default_factory=list)

# Add to the snapshot:
@dataclass
class Snapshot:
    # ... existing fields ...
    aggregation: Optional[AggregationSnapshot] = None
```

- [ ] **Step 5: Update `snapshot_to_dict` to serialize aggregation**

Edit `src/syncfield/viewer/server.py`. Add inside `snapshot_to_dict`:

```python
def _serialize_aggregation(agg) -> dict:
    if agg is None:
        return {"active_job": None, "queue_length": 0, "recent_jobs": []}
    return {
        "active_job": agg.active_job.to_dict() if agg.active_job else None,
        "queue_length": getattr(agg, "queue_length", 0),
        "recent_jobs": [j.to_dict() for j in getattr(agg, "recent_jobs", [])],
    }


def snapshot_to_dict(snapshot) -> dict:
    d = {
        # ... existing fields ...
    }
    d["aggregation"] = _serialize_aggregation(getattr(snapshot, "aggregation", None))
    return d
```

If `snapshot_to_dict` already has a long body, place the aggregation injection at the end of the dict construction.

- [ ] **Step 6: Wire the aggregation queue listener to update state**

Find where the viewer state is constructed and updated (likely in `server.py` or `state.py`). Add:

```python
from syncfield.adapters.insta360_go3s.stream import _global_aggregation_queue

_recent_jobs: list[AggregationProgress] = []
_active: Optional[AggregationProgress] = None


def _on_aggregation_progress(progress: AggregationProgress) -> None:
    nonlocal_state.aggregation = AggregationSnapshot(
        active_job=progress if progress.state == AggregationState.RUNNING else None,
        queue_length=0,  # populated by queue if exposed; otherwise stays 0
        recent_jobs=_recent_jobs[-5:],
    )
    if progress.state in (AggregationState.COMPLETED, AggregationState.FAILED):
        _recent_jobs.append(progress)


_global_aggregation_queue().subscribe(_on_aggregation_progress)
```

If the viewer module shape doesn't permit module-level mutable state cleanly, attach the listener inside the viewer's `start()` / `__init__` and store the snapshot on the existing state object.

- [ ] **Step 7: Run tests**

```
uv run pytest tests/unit/test_viewer_aggregation_snapshot.py -v
```
Expected: PASS.

- [ ] **Step 8: Commit**

```
git add src/syncfield/viewer/server.py src/syncfield/viewer/state.py tests/unit/test_viewer_aggregation_snapshot.py
git commit -m "feat(viewer): expose aggregation state in WS snapshot"
```

---

### Task 14: Viewer control commands for aggregation

**Files:**
- Modify: `src/syncfield/viewer/server.py` (the WS `/ws/control` handler)
- Test: `tests/unit/test_viewer_aggregation_commands.py` (new)

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_viewer_aggregation_commands.py`:

```python
from unittest.mock import MagicMock

from syncfield.viewer.server import handle_control_command


def test_aggregate_episode_dispatches_to_orchestrator():
    orch = MagicMock()
    handle_control_command(orch, {"command": "aggregate_episode", "episode_id": "ep_x"})
    orch.aggregate_episode.assert_called_once_with("ep_x")


def test_retry_aggregation_dispatches_to_queue():
    orch = MagicMock()
    handle_control_command(orch, {"command": "retry_aggregation", "job_id": "agg_x"})
    orch.retry_aggregation.assert_called_once_with("agg_x")


def test_unknown_command_returns_error():
    orch = MagicMock()
    result = handle_control_command(orch, {"command": "no_such_command"})
    assert result["ok"] is False
    assert "unknown" in result["error"].lower()
```

- [ ] **Step 2: Run to confirm failure**

```
uv run pytest tests/unit/test_viewer_aggregation_commands.py -v
```
Expected: ImportError on `handle_control_command` (if it doesn't exist in this form, locate the WS handler).

- [ ] **Step 3: Implement / extend the command dispatcher**

Find the existing WS command handler in `src/syncfield/viewer/server.py`. Extract the `command` switch into a `handle_control_command(orchestrator, payload)` helper if it isn't already. Add the new commands:

```python
def handle_control_command(orchestrator, payload: dict) -> dict:
    cmd = payload.get("command")
    try:
        if cmd == "aggregate_episode":
            orchestrator.aggregate_episode(payload["episode_id"])
            return {"ok": True}
        if cmd == "retry_aggregation":
            orchestrator.retry_aggregation(payload["job_id"])
            return {"ok": True}
        if cmd == "cancel_aggregation":
            orchestrator.cancel_aggregation(payload["job_id"])
            return {"ok": True}
        # ... existing commands ...
        return {"ok": False, "error": f"unknown command: {cmd}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
```

Wire the WS `/ws/control` handler to call this. Then implement matching methods on `SessionOrchestrator`:

```python
# In src/syncfield/orchestrator.py:

def aggregate_episode(self, episode_id: str) -> None:
    """Trigger on-demand aggregation for an episode that is pending."""
    from syncfield.adapters.insta360_go3s.stream import _global_aggregation_queue
    queue = _global_aggregation_queue()
    # Locate the pending job for the episode (search streams for matching episode_dir)
    for stream in self._streams:
        pending = getattr(stream, "pending_aggregation_job", None)
        if pending is not None and pending.episode_id == episode_id:
            queue.enqueue(pending)
            return
    raise KeyError(f"No pending aggregation for episode {episode_id}")


def retry_aggregation(self, job_id: str) -> None:
    from syncfield.adapters.insta360_go3s.stream import _global_aggregation_queue
    _global_aggregation_queue().retry(job_id)


def cancel_aggregation(self, job_id: str) -> None:
    from syncfield.adapters.insta360_go3s.stream import _global_aggregation_queue
    queue = _global_aggregation_queue()
    # Cancel is best-effort; a running job continues but no new ones queue.
    raise NotImplementedError("cancel_aggregation deferred to v2")
```

- [ ] **Step 4: Run tests**

```
uv run pytest tests/unit/test_viewer_aggregation_commands.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/syncfield/viewer/server.py src/syncfield/orchestrator.py tests/unit/test_viewer_aggregation_commands.py
git commit -m "feat(viewer): add aggregation control commands"
```

---

## Phase E — Viewer Frontend

### Task 15: `StandaloneRecorderPanel` component

**Files:**
- Create: `src/syncfield/viewer/frontend/src/components/standalone-recorder-panel.tsx`
- Modify: `src/syncfield/viewer/frontend/src/components/stream-card.tsx` (add dispatcher branch)

- [ ] **Step 1: Read the existing `stream-card.tsx` to understand the props shape**

```
cat src/syncfield/viewer/frontend/src/components/stream-card.tsx
```

Identify the props passed in for video streams (`stream`, `capabilities`, etc.). Note exact prop names; reuse them.

- [ ] **Step 2: Implement `StandaloneRecorderPanel`**

Create `src/syncfield/viewer/frontend/src/components/standalone-recorder-panel.tsx`:

```tsx
import { useMemo } from "react";

type AggregationState = "pending" | "running" | "completed" | "failed";

type Stream = {
  id: string;
  name?: string;
  state: "idle" | "connected" | "recording" | "stopped";
  recordedSeconds?: number;
  lastEpisodeId?: string;
  lastEpisodeBytes?: number;
};

type AggregationForStream = {
  state: AggregationState;
  currentBytes: number;
  totalBytes: number;
} | null;

type Props = {
  stream: Stream;
  aggregation: AggregationForStream;
  onRetry?: () => void;
};

export function StandaloneRecorderPanel({ stream, aggregation, onRetry }: Props) {
  const status = useMemo(() => deriveStatus(stream, aggregation), [stream, aggregation]);

  return (
    <div className="standalone-recorder-panel">
      <header className="srp-header">
        <span className="srp-title">{stream.name ?? stream.id}</span>
        <span className={`srp-dot srp-dot-${status.dot}`} aria-label={status.label} />
      </header>

      <div className="srp-body">
        <CameraGlyph />
        <div className="srp-primary">Standalone recorder</div>
        <div className="srp-secondary">Live preview unavailable</div>
        <div className="srp-status">{renderStatusRow(status, onRetry)}</div>
      </div>

      <footer className="srp-footer">
        {stream.lastEpisodeId && (
          <span>
            Last episode: <code>{stream.lastEpisodeId}</code>
            {stream.lastEpisodeBytes ? ` · ${formatBytes(stream.lastEpisodeBytes)}` : ""}
          </span>
        )}
      </footer>
    </div>
  );
}

function deriveStatus(stream: Stream, agg: AggregationForStream) {
  if (stream.state === "recording") {
    return {
      dot: "rec",
      label: "recording",
      kind: "recording" as const,
      seconds: stream.recordedSeconds ?? 0,
    };
  }
  if (agg?.state === "running") {
    return {
      dot: "agg",
      label: "aggregating",
      kind: "aggregating" as const,
      currentBytes: agg.currentBytes,
      totalBytes: agg.totalBytes,
    };
  }
  if (agg?.state === "failed") {
    return { dot: "fail", label: "failed", kind: "failed" as const };
  }
  if (agg?.state === "completed") {
    return { dot: "ok", label: "ready", kind: "ready" as const };
  }
  return { dot: "idle", label: "idle", kind: "idle" as const };
}

function renderStatusRow(status: ReturnType<typeof deriveStatus>, onRetry?: () => void) {
  switch (status.kind) {
    case "recording":
      return <span>● Recording {formatDuration(status.seconds)}</span>;
    case "aggregating":
      return (
        <span>
          ↓ Aggregating {percent(status.currentBytes, status.totalBytes)}% (
          {formatBytes(status.currentBytes)} / {formatBytes(status.totalBytes)})
        </span>
      );
    case "ready":
      return <span>✓ Ready</span>;
    case "failed":
      return (
        <span>
          ⚠ Failed
          {onRetry && (
            <button type="button" className="srp-retry" onClick={onRetry}>
              Retry
            </button>
          )}
        </span>
      );
    case "idle":
    default:
      return <span className="srp-muted">Idle</span>;
  }
}

function CameraGlyph() {
  return (
    <svg width="48" height="48" viewBox="0 0 24 24" aria-hidden="true">
      <path
        fill="currentColor"
        opacity="0.4"
        d="M9 4l-1.5 2H4a2 2 0 00-2 2v10a2 2 0 002 2h16a2 2 0 002-2V8a2 2 0 00-2-2h-3.5L15 4H9zm3 5a4 4 0 110 8 4 4 0 010-8z"
      />
    </svg>
  );
}

function formatDuration(s: number): string {
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function percent(done: number, total: number): string {
  if (!total) return "0";
  return ((done / total) * 100).toFixed(0);
}
```

- [ ] **Step 3: Add minimal CSS following the existing OpenGraph viewer style**

Locate the viewer stylesheet (`src/syncfield/viewer/frontend/src/styles/` or co-located `.css`). Add:

```css
.standalone-recorder-panel {
  display: flex;
  flex-direction: column;
  padding: 16px;
  border: 1px solid var(--border-subtle, #2a2a2a);
  border-radius: 8px;
  background: var(--panel-bg, #141414);
  min-height: 220px;
  color: var(--text-primary, #e6e6e6);
  font-family: var(--font-sans, "Inter", system-ui, sans-serif);
}
.srp-header { display: flex; justify-content: space-between; align-items: center; }
.srp-title { font-weight: 500; font-size: 14px; }
.srp-dot { width: 8px; height: 8px; border-radius: 50%; }
.srp-dot-idle { background: #555; }
.srp-dot-rec { background: #e25555; }
.srp-dot-agg { background: #e0c054; animation: srpSpin 1.6s linear infinite; }
.srp-dot-ok  { background: #4caf50; }
.srp-dot-fail { background: #e25555; }
.srp-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin-top: 12px;
  text-align: center;
}
.srp-primary { font-size: 13px; color: var(--text-secondary, #a0a0a0); margin-top: 8px; }
.srp-secondary { font-size: 12px; color: var(--text-muted, #707070); }
.srp-status { font-size: 13px; margin-top: 8px; font-variant-numeric: tabular-nums; }
.srp-retry {
  margin-left: 8px;
  background: transparent;
  color: inherit;
  border: 1px solid currentColor;
  border-radius: 4px;
  padding: 2px 8px;
  cursor: pointer;
  font-size: 12px;
}
.srp-footer { font-size: 11px; color: var(--text-muted, #707070); margin-top: 12px; }
.srp-muted { color: var(--text-muted, #707070); }
@keyframes srpSpin {
  0% { opacity: 1; }
  50% { opacity: 0.4; }
  100% { opacity: 1; }
}
```

If the viewer uses Tailwind or another system instead of vanilla CSS, translate to those classes — but keep all colors/sizing tokens consistent with the rest of the UI.

- [ ] **Step 4: Wire the dispatcher in `stream-card.tsx`**

Edit `src/syncfield/viewer/frontend/src/components/stream-card.tsx`. In the `kind === "video"` branch, check capabilities:

```tsx
if (stream.kind === "video") {
  if (stream.capabilities?.live_preview === false) {
    return (
      <StandaloneRecorderPanel
        stream={mapToStandaloneStream(stream)}
        aggregation={mapAggregationForStream(snapshot.aggregation, stream.id)}
        onRetry={() => sendCommand({ command: "retry_aggregation", job_id: snapshot.aggregation?.active_job?.job_id })}
      />
    );
  }
  return <VideoPreview ... />;  // existing
}
```

Provide the small mapping helpers `mapToStandaloneStream` and `mapAggregationForStream` inline in the same file or in a `mappers.ts` next to it. Pull `live_preview` off whatever capabilities object the snapshot exposes (Task 13 added it server-side).

- [ ] **Step 5: Build the frontend**

```
cd src/syncfield/viewer/frontend && yarn build
```
Expected: clean build with no TypeScript errors. Fix any prop-shape mismatches by adjusting the mappers.

- [ ] **Step 6: Commit**

```
git add src/syncfield/viewer/frontend/src/components/standalone-recorder-panel.tsx \
        src/syncfield/viewer/frontend/src/components/stream-card.tsx \
        src/syncfield/viewer/frontend/src/styles
git commit -m "feat(viewer): add StandaloneRecorderPanel for Go3S"
```

---

### Task 16: Aggregation status bar + episode badges

**Files:**
- Create: `src/syncfield/viewer/frontend/src/components/aggregation-status-bar.tsx`
- Modify: viewer top-level layout component (locate during this task) to mount the status bar
- Modify: episode list component (locate via `grep -rn "episode" src/syncfield/viewer/frontend/src --include="*.tsx"`) to add status badge

- [ ] **Step 1: Implement `AggregationStatusBar`**

Create `src/syncfield/viewer/frontend/src/components/aggregation-status-bar.tsx`:

```tsx
type Active = {
  jobId: string;
  episodeId: string;
  state: "running" | "failed";
  currentStreamId: string | null;
  currentBytes: number;
  totalBytes: number;
  camerasDone: number;
  camerasTotal: number;
};

type Props = {
  active: Active | null;
  onRetry: (jobId: string) => void;
  onViewDetails?: (episodeId: string) => void;
};

export function AggregationStatusBar({ active, onRetry, onViewDetails }: Props) {
  if (!active) return null;
  const pct = active.totalBytes
    ? Math.round((active.currentBytes / active.totalBytes) * 100)
    : 0;

  if (active.state === "failed") {
    return (
      <div className="agg-bar agg-bar-fail">
        <span>Aggregation failed for {active.episodeId}</span>
        <button type="button" onClick={() => onRetry(active.jobId)}>Retry</button>
      </div>
    );
  }

  return (
    <div className="agg-bar agg-bar-running">
      <span>
        Aggregating <code>{active.episodeId}</code>
        {active.currentStreamId && ` · ${active.currentStreamId}`}
        {` (${active.camerasDone}/${active.camerasTotal})`}
      </span>
      <div className="agg-bar-progress">
        <div className="agg-bar-fill" style={{ width: `${pct}%` }} />
      </div>
      <span>
        {pct}% · {formatBytes(active.currentBytes)} / {formatBytes(active.totalBytes)}
      </span>
      {onViewDetails && (
        <button type="button" onClick={() => onViewDetails(active.episodeId)}>
          View Details
        </button>
      )}
    </div>
  );
}

function formatBytes(n: number): string {
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}
```

CSS additions:

```css
.agg-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 16px;
  border-bottom: 1px solid var(--border-subtle, #2a2a2a);
  background: var(--panel-bg, #141414);
  font-size: 12px;
  color: var(--text-secondary, #a0a0a0);
  font-variant-numeric: tabular-nums;
}
.agg-bar code { color: var(--text-primary, #e6e6e6); }
.agg-bar-progress {
  flex: 1;
  height: 4px;
  background: var(--border-subtle, #2a2a2a);
  border-radius: 2px;
  overflow: hidden;
}
.agg-bar-fill {
  height: 100%;
  background: var(--accent, #5b9dd9);
  transition: width 200ms ease-out;
}
.agg-bar-fail { color: #e25555; }
.agg-bar-fail button { color: inherit; border: 1px solid currentColor; background: transparent; padding: 2px 10px; border-radius: 4px; cursor: pointer; }
.agg-bar button { color: inherit; background: transparent; border: 1px solid var(--border-subtle, #2a2a2a); padding: 2px 8px; border-radius: 4px; cursor: pointer; }
```

- [ ] **Step 2: Mount the status bar in the top-level layout**

Locate the top-level viewer layout component (likely `App.tsx`, `Layout.tsx`, or `MainView.tsx`):

```
grep -rn "WebSocket\|/ws/control" src/syncfield/viewer/frontend/src --include="*.tsx"
```

Insert just below the header and above the main content:

```tsx
<AggregationStatusBar
  active={mapActiveAggregation(snapshot.aggregation)}
  onRetry={(jobId) => sendCommand({ command: "retry_aggregation", job_id: jobId })}
/>
```

Provide `mapActiveAggregation` helper that adapts the WS payload shape (Task 13's `aggregation.active_job`) into the bar's `Active` type.

- [ ] **Step 3: Add aggregation status badge to episode list rows**

Find the episode-list row component. For each episode, show one of:

```tsx
function AggregationBadge({ state, percent }: { state?: string; percent?: number }) {
  if (!state || state === "completed") return <span className="badge badge-ok">Ready</span>;
  if (state === "running") return <span className="badge badge-progress">Aggregating {percent ?? 0}%</span>;
  if (state === "failed") return <span className="badge badge-fail">Failed</span>;
  return <span className="badge badge-pending">Pending</span>;
}
```

Source the state from the same `snapshot.aggregation` data — for episodes not currently active, derive state from `recent_jobs` keyed by `episode_id`.

- [ ] **Step 4: Build the frontend**

```
cd src/syncfield/viewer/frontend && yarn build
```
Expected: clean build.

- [ ] **Step 5: Commit**

```
git add src/syncfield/viewer/frontend/src/components/aggregation-status-bar.tsx \
        src/syncfield/viewer/frontend/src/styles
git commit -m "feat(viewer): add aggregation status bar and episode badges"
```

---

### Task 17: Discovery modal extension for Go3S

**Files:**
- Modify: `src/syncfield/viewer/frontend/src/components/discovery-modal.tsx`

- [ ] **Step 1: Read the modal to find the device row rendering**

```
cat src/syncfield/viewer/frontend/src/components/discovery-modal.tsx
```

Locate the function that decides what type of "Add" CTA to render per discovered device.

- [ ] **Step 2: Add Go3S name pattern detection**

In the device row component, add:

```tsx
function isGo3SDevice(name: string | undefined): boolean {
  if (!name) return false;
  const lower = name.toLowerCase();
  return lower.includes("go 3") || lower.includes("go3");
}

function deviceTypeLabel(device: DiscoveredDevice): string {
  if (isGo3SDevice(device.name)) return "Insta360 Go3S";
  // ... existing device type detection ...
  return "Unknown device";
}

function addCta(device: DiscoveredDevice, onAdd: (kind: string) => void) {
  if (isGo3SDevice(device.name)) {
    return (
      <button type="button" onClick={() => onAdd("go3s")}>
        Add as Go3S camera
      </button>
    );
  }
  // ... existing CTAs ...
}
```

The `onAdd("go3s")` flow must call a new viewer-server command `add_go3s_stream` with `{address, name}`. Add that command to `handle_control_command` in `src/syncfield/viewer/server.py`:

```python
if cmd == "add_go3s_stream":
    from syncfield.adapters.insta360_go3s import Go3SStream
    stream = Go3SStream(
        stream_id=payload.get("stream_id") or _next_default_id(orchestrator, "go3s_cam"),
        ble_address=payload["address"],
        output_dir=orchestrator.output_dir,
    )
    orchestrator.add(stream)
    return {"ok": True, "stream_id": stream.id}
```

Define `_next_default_id(orchestrator, prefix)` as a small helper that scans existing stream IDs for `prefix_N` and returns the next available index.

- [ ] **Step 3: Build the frontend**

```
cd src/syncfield/viewer/frontend && yarn build
```
Expected: clean build.

- [ ] **Step 4: Commit**

```
git add src/syncfield/viewer/frontend/src/components/discovery-modal.tsx src/syncfield/viewer/server.py
git commit -m "feat(viewer): recognize Go3S in discovery modal"
```

---

## Phase F — Integration & Documentation

### Task 18: Integration test — full E2E session

**Files:**
- Create: `tests/integration/insta360_go3s/test_session_e2e.py`

- [ ] **Step 1: Write the test (it will drive the design of the in-memory test doubles)**

Create `tests/integration/insta360_go3s/test_session_e2e.py`:

```python
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from syncfield.adapters.insta360_go3s import Go3SStream
from syncfield.adapters.insta360_go3s.aggregation.queue import (
    AggregationDownloader,
    AggregationQueue,
)
from syncfield.adapters.insta360_go3s.aggregation.types import AggregationState
from syncfield.adapters.insta360_go3s.ble.camera import CaptureResult
from syncfield.orchestrator import SessionOrchestrator


class FakeBleCamera:
    def __init__(self, address: str):
        self.address = address
    async def connect(self, sync_timeout: float = 2.0, auth_timeout: float = 1.0): pass
    async def set_video_mode(self): pass
    async def start_capture(self) -> int: return 12345
    async def stop_capture(self) -> CaptureResult:
        return CaptureResult(file_path="/DCIM/Camera01/VID_E2E.mp4", ack_host_ns=23456)
    async def disconnect(self): pass


class FakeDownloader(AggregationDownloader):
    async def run(self, camera, target_dir, on_chunk):
        target = target_dir / camera.local_filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"VID_E2E_FAKE_CONTENT")
        on_chunk(camera.stream_id, len(b"VID_E2E_FAKE_CONTENT"), len(b"VID_E2E_FAKE_CONTENT"))


@pytest.mark.asyncio
async def test_e2e_record_then_aggregate(tmp_path):
    queue = AggregationQueue(downloader=FakeDownloader())
    await queue.start()
    try:
        with (
            patch("syncfield.adapters.insta360_go3s.stream.Go3SBLECamera", FakeBleCamera),
            patch(
                "syncfield.adapters.insta360_go3s.stream._global_aggregation_queue",
                lambda: queue,
            ),
        ):
            ep_dir = tmp_path / "ep_e2e"
            ep_dir.mkdir()
            session = SessionOrchestrator(host_id="mac", output_dir=tmp_path)
            stream = Go3SStream(
                stream_id="overhead",
                ble_address="AA:BB:CC:DD:EE:FF",
                output_dir=ep_dir,
            )
            session.add(stream)
            stream.prepare()
            stream.connect()
            stream.start_recording(session_clock=None)  # type: ignore[arg-type]
            report = stream.stop_recording()
            stream.disconnect()

            assert report.status == "pending_aggregation"
            assert stream.pending_aggregation_job is not None

            # Wait for the queue to flush the job
            for _ in range(50):
                if (ep_dir / "overhead.mp4").exists():
                    break
                await asyncio.sleep(0.1)
            assert (ep_dir / "overhead.mp4").exists()
            manifest = json.loads((ep_dir / "aggregation.json").read_text())
            assert manifest["state"] == "completed"
            assert manifest["cameras"][0]["done"] is True
    finally:
        await queue.shutdown()
```

- [ ] **Step 2: Run**

```
uv run pytest tests/integration/insta360_go3s/test_session_e2e.py -v
```
Expected: PASS.

If failures arise from `Go3SStream._enqueue_job` not running the queue under pytest-asyncio's event loop, adjust `_enqueue_job` to detect a running loop and just call `queue.enqueue(job)` (the worker is already started by the test).

- [ ] **Step 3: Commit**

```
git add tests/integration/insta360_go3s/test_session_e2e.py src/syncfield/adapters/insta360_go3s/stream.py
git commit -m "test(go3s): integration test for full record-then-aggregate"
```

---

### Task 19: Integration test — aggregation during recording does not interfere

**Files:**
- Create: `tests/integration/insta360_go3s/test_aggregation_during_recording.py`

- [ ] **Step 1: Write test**

```python
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from syncfield.adapters.insta360_go3s import Go3SStream
from syncfield.adapters.insta360_go3s.aggregation.queue import (
    AggregationDownloader,
    AggregationQueue,
)
from syncfield.adapters.insta360_go3s.ble.camera import CaptureResult
from syncfield.orchestrator import SessionOrchestrator


class SlowDownloader(AggregationDownloader):
    """Holds the WiFi for ~0.5s so a second recording fires while it's busy."""

    def __init__(self):
        self.in_flight = asyncio.Event()
        self.may_finish = asyncio.Event()
        self.completed: list[str] = []

    async def run(self, camera, target_dir, on_chunk):
        self.in_flight.set()
        await self.may_finish.wait()
        target = target_dir / camera.local_filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"X" * 32)
        on_chunk(camera.stream_id, 32, 32)
        self.completed.append(camera.stream_id)


class FakeBleCamera:
    def __init__(self, address): self.address = address
    async def connect(self, sync_timeout=2.0, auth_timeout=1.0): pass
    async def set_video_mode(self): pass
    async def start_capture(self): return 1
    async def stop_capture(self): return CaptureResult(file_path="/DCIM/Camera01/VID.mp4", ack_host_ns=2)
    async def disconnect(self): pass


@pytest.mark.asyncio
async def test_recording_succeeds_while_aggregation_runs(tmp_path):
    downloader = SlowDownloader()
    queue = AggregationQueue(downloader=downloader)
    await queue.start()
    try:
        with (
            patch("syncfield.adapters.insta360_go3s.stream.Go3SBLECamera", FakeBleCamera),
            patch(
                "syncfield.adapters.insta360_go3s.stream._global_aggregation_queue",
                lambda: queue,
            ),
        ):
            session = SessionOrchestrator(host_id="mac", output_dir=tmp_path)
            ep1 = tmp_path / "ep1"; ep1.mkdir()
            ep2 = tmp_path / "ep2"; ep2.mkdir()
            stream = Go3SStream(
                stream_id="overhead",
                ble_address="AA:BB:CC:DD:EE:FF",
                output_dir=ep1,
            )
            session.add(stream)

            # Episode 1
            stream.prepare(); stream.connect()
            stream.start_recording(session_clock=None)  # type: ignore[arg-type]
            stream.stop_recording()  # enqueues episode 1

            # Wait for downloader to be mid-flight
            await asyncio.wait_for(downloader.in_flight.wait(), timeout=2.0)

            # Episode 2 — start while episode 1's download is in-flight
            stream._output_dir = ep2  # simulate orchestrator advancing episode dir
            stream.start_recording(session_clock=None)  # type: ignore[arg-type]
            report2 = stream.stop_recording()
            assert report2.status == "pending_aggregation"

            # Now release the slow downloader so both episodes can finish
            downloader.may_finish.set()

            for _ in range(80):
                if (ep1 / "overhead.mp4").exists() and (ep2 / "overhead.mp4").exists():
                    break
                await asyncio.sleep(0.05)
            assert (ep1 / "overhead.mp4").exists()
            assert (ep2 / "overhead.mp4").exists()
    finally:
        await queue.shutdown()
```

- [ ] **Step 2: Run**

```
uv run pytest tests/integration/insta360_go3s/test_aggregation_during_recording.py -v
```
Expected: PASS.

- [ ] **Step 3: Commit**

```
git add tests/integration/insta360_go3s/test_aggregation_during_recording.py
git commit -m "test(go3s): aggregation does not block subsequent recordings"
```

---

### Task 20: Integration test — atomic failure preserves originals + retry works

**Files:**
- Create: `tests/integration/insta360_go3s/test_atomic_failure.py`

- [ ] **Step 1: Write test**

```python
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from syncfield.adapters.insta360_go3s import Go3SStream
from syncfield.adapters.insta360_go3s.aggregation.queue import (
    AggregationDownloader,
    AggregationQueue,
)
from syncfield.adapters.insta360_go3s.aggregation.types import AggregationState
from syncfield.adapters.insta360_go3s.ble.camera import CaptureResult
from syncfield.orchestrator import SessionOrchestrator


class FlakyDownloader(AggregationDownloader):
    """Fails the first time, succeeds on retry."""

    def __init__(self):
        self.attempts = 0

    async def run(self, camera, target_dir, on_chunk):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("simulated WiFi switch failure")
        target = target_dir / camera.local_filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"OK")
        on_chunk(camera.stream_id, 2, 2)


class FakeBleCamera:
    def __init__(self, address): self.address = address
    async def connect(self, sync_timeout=2.0, auth_timeout=1.0): pass
    async def set_video_mode(self): pass
    async def start_capture(self): return 1
    async def stop_capture(self): return CaptureResult(file_path="/DCIM/Camera01/VID.mp4", ack_host_ns=2)
    async def disconnect(self): pass


@pytest.mark.asyncio
async def test_failure_then_retry(tmp_path):
    downloader = FlakyDownloader()
    queue = AggregationQueue(downloader=downloader)
    await queue.start()
    try:
        with (
            patch("syncfield.adapters.insta360_go3s.stream.Go3SBLECamera", FakeBleCamera),
            patch(
                "syncfield.adapters.insta360_go3s.stream._global_aggregation_queue",
                lambda: queue,
            ),
        ):
            session = SessionOrchestrator(host_id="mac", output_dir=tmp_path)
            ep = tmp_path / "ep_fail"; ep.mkdir()
            stream = Go3SStream(
                stream_id="overhead",
                ble_address="AA:BB:CC:DD:EE:FF",
                output_dir=ep,
            )
            session.add(stream)
            stream.prepare(); stream.connect()
            stream.start_recording(session_clock=None)  # type: ignore[arg-type]
            stream.stop_recording()
            job_id = stream.pending_aggregation_job.job_id

            # Wait for first failure
            for _ in range(40):
                if downloader.attempts >= 1:
                    break
                await asyncio.sleep(0.05)
            # Read aggregation.json — should be FAILED
            import json
            for _ in range(40):
                manifest = json.loads((ep / "aggregation.json").read_text())
                if manifest["state"] == "failed":
                    break
                await asyncio.sleep(0.05)
            assert manifest["state"] == "failed"
            assert not (ep / "overhead.mp4").exists()

            # Retry
            handle = queue.retry(job_id)
            final = await handle.wait()
            assert final.state == AggregationState.COMPLETED
            assert (ep / "overhead.mp4").exists()
    finally:
        await queue.shutdown()
```

- [ ] **Step 2: Run**

```
uv run pytest tests/integration/insta360_go3s/test_atomic_failure.py -v
```
Expected: PASS.

- [ ] **Step 3: Commit**

```
git add tests/integration/insta360_go3s/test_atomic_failure.py
git commit -m "test(go3s): atomic failure preserves originals; retry succeeds"
```

---

### Task 21: Example script + README

**Files:**
- Create: `examples/insta360_go3s/record.py`
- Create: `examples/insta360_go3s/README.md`

- [ ] **Step 1: Write the example script**

Create `examples/insta360_go3s/record.py`:

```python
"""Single-host recording with one Insta360 Go3S camera.

Usage:
    uv run python examples/insta360_go3s/record.py \\
        --address AA:BB:CC:DD:EE:FF \\
        --output ./go3s_output \\
        --duration 10
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import syncfield as sf
from syncfield.adapters.insta360_go3s import Go3SStream


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True, help="Go3S BLE address (MAC or CB UUID)")
    parser.add_argument("--output", type=Path, default=Path("./go3s_output"))
    parser.add_argument("--duration", type=float, default=10.0)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)

    session = sf.SessionOrchestrator(host_id="local", output_dir=args.output)
    session.add(Go3SStream(
        stream_id="overhead",
        ble_address=args.address,
        output_dir=args.output,
    ))

    print(f"[record] starting session, duration={args.duration}s")
    session.start_recording()
    time.sleep(args.duration)
    report = session.stop_recording()
    print(f"[record] stopped; per-stream reports: {report}")
    print("[record] aggregation runs in the background; check the viewer or look in",
          args.output)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the README**

Create `examples/insta360_go3s/README.md`:

````markdown
# Insta360 Go3S example

Records via BLE trigger; downloads files in a background WiFi aggregation
job after the session ends.

## One-time setup

1. **Pair the Go3S** with the laptop using its BLE name (e.g. via the system
   Bluetooth pane). After pairing, the BLE address persists.
2. **Discover the BLE address**:
   ```
   uv run python -c "import asyncio; from bleak import BleakScanner; \\
     print(asyncio.run(BleakScanner.discover()))"
   ```
3. **macOS only**: the first WiFi switch will request Location permission
   (required by `networksetup`). Grant once.

## Run

```
uv run python examples/insta360_go3s/record.py \
    --address AA:BB:CC:DD:EE:FF \
    --output ./go3s_output \
    --duration 10
```

After `stop`, the SDK reports `pending_aggregation` and a background worker
switches the host WiFi to the camera AP, downloads the video file, and
restores the previous network. Episode dir contents:

```
go3s_output/
├── overhead.mp4              ← downloaded
├── aggregation.json          ← per-episode atomic state
├── manifest.json             ← session metadata
└── ...
```

## Multihost note

If you use a `LeaderRole` or `FollowerRole`, the adapter automatically
downgrades the policy to `on_demand` so aggregation does not break lab
WiFi (mDNS) during the session. Trigger aggregation explicitly from the
viewer's "Aggregate now" button after recording wraps.

## Limitations (v1)

- No live preview (the camera does not expose one over the BLE/OSC path).
- No Windows WiFi switching (`NotImplementedError`); BLE-only flows still work.
- Per-camera resolution/fps uses the camera's own UI setting.
- Aggregation across multiple Go3S devices is sequential per episode.
````

- [ ] **Step 3: Commit**

```
git add examples/insta360_go3s
git commit -m "docs(examples): add Insta360 Go3S example"
```

---

### Task 22: Optional dependency declaration

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `aiohttp` to the `camera` optional extra**

Open `pyproject.toml`. Locate `[project.optional-dependencies]` (or `[tool.uv.optional-dependencies]`). Find or create the `camera` extra:

```toml
[project.optional-dependencies]
camera = [
    "bleak>=0.21",
    "aiohttp>=3.9",
]
```

If a `camera` extra already exists with a different shape, append `aiohttp>=3.9` to its list.

- [ ] **Step 2: Resolve the lock**

```
uv lock
uv sync --extra camera --extra dev
```

- [ ] **Step 3: Run the full Go3S test suite**

```
uv run pytest tests/unit/adapters/insta360_go3s tests/integration/insta360_go3s -v
```
Expected: all PASS.

- [ ] **Step 4: Run the entire test suite to check for regressions**

```
uv run pytest -q
```
Expected: no new failures vs. baseline. (Pre-existing failures unrelated to Go3S are out of scope; flag them but do not block.)

- [ ] **Step 5: Commit**

```
git add pyproject.toml uv.lock
git commit -m "build: add aiohttp to camera optional extra"
```

---

## Self-Review Checklist

After all tasks complete, before declaring done:

- [ ] Spec section "Goals" — every goal has a corresponding task or set of tasks.
- [ ] Spec section "Components" — every new file in the spec exists in this plan.
- [ ] Spec section "Error Handling & Atomicity" — table rows are covered by Tasks 8, 9, 18, 19, 20.
- [ ] Spec section "Viewer UX" — Tasks 15, 16, 17 deliver standalone panel, status bar, badges, discovery extension.
- [ ] Spec section "Multihost autodetection" — Task 12.
- [ ] Spec section "Configuration Surface" — Task 21.
- [ ] No `TBD`, `TODO`, or "implement later" tokens in this plan (run `grep -E "TBD|TODO|implement later" docs/superpowers/plans/2026-04-14-insta360-go3s-adapter.md`).
- [ ] Type names consistent across tasks (`AggregationCameraSpec`, `AggregationJob`, `AggregationProgress`, `Go3SBLECamera`, `CaptureResult`, `OscHttpClient`, `OscDownloadError`, `WifiSwitcher`, `Go3SStream`).
- [ ] Every test step shows the actual test code; every implementation step shows the actual implementation code (or a precise port-from-reference instruction with file path).
- [ ] All commits are scoped: tests + impl together per task; no plan-wide WIP commits.
