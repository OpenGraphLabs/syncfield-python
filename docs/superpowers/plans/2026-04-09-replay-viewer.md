# Replay Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `sf.replay.launch(session_dir)` — a local browser-based viewer that opens a saved SyncField session, plays its streams back together, and lets the user toggle between Before (raw) and After (synced) to verify sync quality.

**Architecture:** Python `[replay]` extra (Starlette + uvicorn) serves a pre-built React SPA over localhost. The SPA is a port of egonaut/web's `DataReviewPage` with Supabase/auth/router stripped out and data fetched from local HTTP endpoints. Pre-built `static/` lives under `src/syncfield/replay/static/` and is committed to the repo so end users never need Node.

**Tech Stack:** Python 3.9+, Starlette, uvicorn, hatchling. React 19, Vite 8, Tailwind 4, TypeScript, yarn.

**Spec:** `docs/superpowers/specs/2026-04-09-replay-viewer-design.md`

---

## File structure

### Python (new)

| File | Responsibility |
|---|---|
| `src/syncfield/replay/__init__.py` | Public `launch()`, dep-check on import |
| `src/syncfield/replay/loader.py` | `ReplayManifest`, `ReplayStream`, `load_session()` |
| `src/syncfield/replay/_handler.py` | `safe_resolve()` path-traversal guard, custom `RangedFileResponse` if needed |
| `src/syncfield/replay/server.py` | `ReplayServer` class, route definitions |
| `src/syncfield/replay/__main__.py` | `python -m syncfield.replay.server --dev` dev entry |
| `src/syncfield/replay/static/` | Built frontend (placeholder until web build) |

### Web (new) — `src/syncfield/replay/_web/`

| File | Responsibility |
|---|---|
| `package.json` | yarn deps and scripts |
| `vite.config.ts` | build outDir = `../static`, dev proxy |
| `tsconfig.json`, `tsconfig.node.json` | TS config (copied from egonaut) |
| `tailwind.config.ts` | Tailwind 4 with egonaut design tokens |
| `index.html` | SPA entry |
| `src/main.tsx` | React root |
| `src/App.tsx` | Replaces `DataReviewPage` shell |
| `src/types.ts` | `SessionManifest`, `SyncReport`, `ReplayStream` types |
| `src/hooks/useReplaySession.ts` | Fetches `/api/session` + `/api/sync-report` |
| `src/hooks/useBeforeAfter.ts` | Mode state + offset lookup |
| `src/components/VideoArea.tsx` | Ported from egonaut, offset-aware |
| `src/components/HeroVideo.tsx` | Ported as-is |
| `src/components/SecondaryVideo.tsx` | Ported as-is |
| `src/components/SyncReportPanel.tsx` | NEW — per-stream offset/quality cards |
| `src/components/BeforeAfterToggle.tsx` | NEW — segmented control + keyboard `B` |
| `src/components/SensorChartPanel.tsx` | NEW — generalized minimal sensor charts |
| `src/components/ContactTimeline.tsx` | Ported, conditional |
| `src/components/TactilePanel.tsx` | Ported, conditional |
| `src/lib/sensorParser.ts` | Ported as-is from egonaut |
| `src/index.css` | Ported tailwind directives + design tokens |

### Modified

| File | Change |
|---|---|
| `pyproject.toml` | Add `[replay]` extra, force-include `static/`, update `[all]` |
| `Makefile` | Add `replay-web-install`, `replay-web-build`, `replay-web-dev` |
| `README.md` | Add "Replay a synced session" section |

### Tests (new)

| File | Coverage |
|---|---|
| `tests/unit/replay/__init__.py` | Empty marker |
| `tests/unit/replay/conftest.py` | `synthetic_session` fixture |
| `tests/unit/replay/test_loader.py` | `load_session` happy path + edge cases |
| `tests/unit/replay/test_handler.py` | `safe_resolve()` path traversal cases |
| `tests/unit/replay/test_server.py` | Starlette `TestClient` against routes |
| `tests/unit/replay/test_launch_smoke.py` | Background-thread launch + requests |

---

## Phase 1 — Python loader

### Task 1: Create the `syncfield.replay` package skeleton

**Files:**
- Create: `src/syncfield/replay/__init__.py`
- Create: `src/syncfield/replay/static/.gitkeep`
- Create: `tests/unit/replay/__init__.py`

- [ ] **Step 1: Create the package directories and empty init**

```bash
mkdir -p src/syncfield/replay/static
mkdir -p tests/unit/replay
touch src/syncfield/replay/static/.gitkeep
touch tests/unit/replay/__init__.py
```

Create `src/syncfield/replay/__init__.py`:

```python
"""SyncField replay viewer — local browser-based session playback.

Open a previously recorded session in your default browser to verify
sync quality (per-stream offsets, Before/After comparison, sync report).

Usage::

    import syncfield as sf
    sf.replay.launch("./data/session_2026-04-09T14-49")

Requires the ``replay`` extra::

    pip install 'syncfield[replay]'
"""

from __future__ import annotations

try:
    import starlette  # noqa: F401
    import uvicorn  # noqa: F401
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "syncfield.replay requires the 'replay' extra. "
        "Install with `pip install 'syncfield[replay]'`."
    ) from exc

# Re-exported once server.py exists in Task 5.
__all__ = ["launch"]
```

- [ ] **Step 2: Verify the package imports**

Run: `python -c "import syncfield.replay" 2>&1`

Expected: ImportError about the `replay` extra (because starlette/uvicorn are not installed yet). That's fine — proves the dep guard fires.

- [ ] **Step 3: Install the dev deps so the rest of the plan works**

Run:
```bash
uv pip install 'starlette>=0.36' 'uvicorn>=0.27' 'httpx>=0.27'
```

(`httpx` is needed for Starlette's `TestClient` in later tests.)

- [ ] **Step 4: Verify the import now succeeds**

Run: `python -c "import syncfield.replay; print(syncfield.replay.__doc__.splitlines()[0])"`

Expected: `SyncField replay viewer — local browser-based session playback.`

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/replay tests/unit/replay
git commit -m "feat(replay): scaffold syncfield.replay package with dep guard"
```

---

### Task 2: `loader.py` — session-folder parsing

**Files:**
- Create: `src/syncfield/replay/loader.py`
- Create: `tests/unit/replay/conftest.py`
- Create: `tests/unit/replay/test_loader.py`

- [ ] **Step 1: Create the synthetic-session fixture**

Write `tests/unit/replay/conftest.py`:

```python
"""Fixtures for replay loader and server tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


@pytest.fixture
def synthetic_session(tmp_path: Path) -> Path:
    """Build a minimal session folder on disk and return its path."""
    session = tmp_path / "session_test"
    session.mkdir()

    _write_json(
        session / "manifest.json",
        {
            "sdk_version": "0.2.0",
            "host_id": "test_rig",
            "streams": {
                "cam_ego": {
                    "kind": "video",
                    "capabilities": {
                        "provides_audio_track": True,
                        "supports_precise_timestamps": True,
                        "is_removable": False,
                        "produces_file": True,
                    },
                    "status": "completed",
                    "frame_count": 60,
                },
                "wrist_imu": {
                    "kind": "sensor",
                    "capabilities": {
                        "provides_audio_track": False,
                        "supports_precise_timestamps": True,
                        "is_removable": False,
                        "produces_file": False,
                    },
                    "status": "completed",
                    "frame_count": 600,
                },
            },
        },
    )

    _write_json(
        session / "sync_point.json",
        {
            "sdk_version": "0.2.0",
            "monotonic_ns": 100_000_000_000,
            "wall_clock_ns": 1_775_000_000_000_000_000,
            "host_id": "test_rig",
            "timestamp_ms": 1_775_000_000_000,
            "iso_datetime": "2026-04-09T00:00:00",
        },
    )

    # Fake "video" file — content is irrelevant to the loader, only the
    # path matters. Use a few bytes so Range tests have something to slice.
    (session / "cam_ego.mp4").write_bytes(b"\x00MP4FAKE\x00" * 64)

    # Sensor jsonl with two samples
    (session / "wrist_imu.jsonl").write_text(
        '{"t_ns":0,"channels":{"ax":0.1}}\n'
        '{"t_ns":1000000,"channels":{"ax":0.2}}\n'
    )

    return session


@pytest.fixture
def synced_session(synthetic_session: Path) -> Path:
    """A session that also has a synced/sync_report.json."""
    synced = synthetic_session / "synced"
    synced.mkdir()
    _write_json(
        synced / "sync_report.json",
        {
            "streams": {
                "cam_ego": {
                    "offset_seconds": 0.012,
                    "confidence": 0.97,
                    "quality": "excellent",
                },
                "wrist_imu": {
                    "offset_seconds": -0.034,
                    "confidence": 0.81,
                    "quality": "good",
                },
            },
        },
    )
    return synthetic_session
```

- [ ] **Step 2: Write the failing loader test**

Write `tests/unit/replay/test_loader.py`:

```python
"""Unit tests for syncfield.replay.loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncfield.replay.loader import ReplayManifest, load_session


def test_load_session_returns_manifest(synthetic_session: Path) -> None:
    manifest = load_session(synthetic_session)

    assert isinstance(manifest, ReplayManifest)
    assert manifest.session_dir == synthetic_session
    assert manifest.host_id == "test_rig"
    assert manifest.sync_point["host_id"] == "test_rig"
    assert manifest.sync_report is None
    assert manifest.has_frame_map is False


def test_load_session_finds_video_and_sensor_streams(
    synthetic_session: Path,
) -> None:
    manifest = load_session(synthetic_session)
    by_id = {s.id: s for s in manifest.streams}

    assert set(by_id) == {"cam_ego", "wrist_imu"}

    cam = by_id["cam_ego"]
    assert cam.kind == "video"
    assert cam.media_url == "/media/cam_ego"
    assert cam.media_path == synthetic_session / "cam_ego.mp4"
    assert cam.frame_count == 60

    imu = by_id["wrist_imu"]
    assert imu.kind == "sensor"
    assert imu.media_url is None
    assert imu.data_url == "/data/wrist_imu.jsonl"
    assert imu.frame_count == 600


def test_load_session_with_sync_report(synced_session: Path) -> None:
    manifest = load_session(synced_session)

    assert manifest.sync_report is not None
    assert manifest.sync_report["streams"]["cam_ego"]["quality"] == "excellent"


def test_load_session_missing_manifest_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_session(tmp_path / "does_not_exist")


def test_load_session_missing_sync_point_is_optional(
    synthetic_session: Path,
) -> None:
    (synthetic_session / "sync_point.json").unlink()
    manifest = load_session(synthetic_session)
    assert manifest.sync_point == {}
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/unit/replay/test_loader.py -v`

Expected: All five tests fail with `ModuleNotFoundError: No module named 'syncfield.replay.loader'`.

- [ ] **Step 4: Implement `loader.py`**

Write `src/syncfield/replay/loader.py`:

```python
"""Session-folder loader for the replay viewer.

Reads a directory written by ``syncfield.writer`` and produces a
:class:`ReplayManifest` that the HTTP server can serve as JSON.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

StreamKind = Literal["video", "sensor", "custom"]


@dataclass(frozen=True)
class ReplayStream:
    """One stream's metadata + on-disk locations.

    ``media_path`` is kept on the Python side for the file-serving
    handler; it is intentionally excluded from the JSON view of the
    manifest (see :meth:`ReplayManifest.to_json`).
    """

    id: str
    kind: StreamKind
    media_url: Optional[str]
    media_path: Optional[Path]
    data_url: Optional[str]
    data_path: Optional[Path]
    frame_count: int


@dataclass(frozen=True)
class ReplayManifest:
    """Everything the SPA needs to render a session, in one struct."""

    session_dir: Path
    host_id: str
    sync_point: dict
    streams: list[ReplayStream]
    sync_report: Optional[dict]
    has_frame_map: bool

    def to_json(self) -> dict[str, Any]:
        """Serializable view — strips Path fields the SPA does not need."""
        return {
            "host_id": self.host_id,
            "sync_point": self.sync_point,
            "has_frame_map": self.has_frame_map,
            "streams": [
                {
                    "id": s.id,
                    "kind": s.kind,
                    "media_url": s.media_url,
                    "data_url": s.data_url,
                    "frame_count": s.frame_count,
                }
                for s in self.streams
            ],
        }


def load_session(session_dir: Path) -> ReplayManifest:
    """Read a session folder and return its :class:`ReplayManifest`.

    Raises:
        FileNotFoundError: if ``session_dir/manifest.json`` does not exist.
    """
    session_dir = Path(session_dir)
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"manifest.json not found in {session_dir}"
        )

    raw = json.loads(manifest_path.read_text())
    host_id = raw.get("host_id", "")
    streams_raw: dict = raw.get("streams", {})

    streams: list[ReplayStream] = []
    for stream_id, info in streams_raw.items():
        kind: StreamKind = info.get("kind", "custom")
        frame_count = int(info.get("frame_count", 0) or 0)

        media_path: Optional[Path] = None
        media_url: Optional[str] = None
        data_path: Optional[Path] = None
        data_url: Optional[str] = None

        if kind == "video":
            mp4 = session_dir / f"{stream_id}.mp4"
            if mp4.is_file():
                media_path = mp4
                media_url = f"/media/{stream_id}"

        sensor_jsonl = session_dir / f"{stream_id}.jsonl"
        if sensor_jsonl.is_file():
            data_path = sensor_jsonl
            data_url = f"/data/{stream_id}.jsonl"

        streams.append(
            ReplayStream(
                id=stream_id,
                kind=kind,
                media_url=media_url,
                media_path=media_path,
                data_url=data_url,
                data_path=data_path,
                frame_count=frame_count,
            )
        )

    sync_point: dict = {}
    sp_path = session_dir / "sync_point.json"
    if sp_path.is_file():
        sync_point = json.loads(sp_path.read_text())
    else:
        logger.warning("sync_point.json missing in %s", session_dir)

    sync_report: Optional[dict] = None
    sr_path = session_dir / "synced" / "sync_report.json"
    if sr_path.is_file():
        sync_report = json.loads(sr_path.read_text())

    has_frame_map = (session_dir / "synced" / "frame_map.jsonl").is_file()

    return ReplayManifest(
        session_dir=session_dir,
        host_id=host_id,
        sync_point=sync_point,
        streams=streams,
        sync_report=sync_report,
        has_frame_map=has_frame_map,
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/unit/replay/test_loader.py -v`

Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add src/syncfield/replay/loader.py tests/unit/replay/conftest.py tests/unit/replay/test_loader.py
git commit -m "feat(replay): session folder loader with manifest + sync_report parsing"
```

---

### Task 3: `_handler.py` — path-traversal guard

**Files:**
- Create: `src/syncfield/replay/_handler.py`
- Create: `tests/unit/replay/test_handler.py`

- [ ] **Step 1: Write the failing security test**

Write `tests/unit/replay/test_handler.py`:

```python
"""Path-traversal protection for the media/data routes."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncfield.replay._handler import UnsafePathError, safe_resolve


def test_safe_resolve_accepts_in_root(tmp_path: Path) -> None:
    target = tmp_path / "ok.mp4"
    target.write_bytes(b"x")
    assert safe_resolve(tmp_path, "ok.mp4") == target.resolve()


def test_safe_resolve_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        safe_resolve(tmp_path, "../etc/passwd")


def test_safe_resolve_rejects_absolute(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        safe_resolve(tmp_path, "/etc/passwd")


def test_safe_resolve_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_target"
    outside.write_text("secret")
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(UnsafePathError):
        safe_resolve(tmp_path, "link")
    outside.unlink()


def test_safe_resolve_missing_file_returns_none(tmp_path: Path) -> None:
    assert safe_resolve(tmp_path, "nope.mp4") is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/replay/test_handler.py -v`

Expected: All 5 tests fail with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `_handler.py`**

Write `src/syncfield/replay/_handler.py`:

```python
"""Internal helpers for the replay HTTP server.

Right now this is just :func:`safe_resolve` — the path-traversal guard
that every file-serving route must funnel through. Kept in its own
module so the security-sensitive surface is small and easy to audit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


class UnsafePathError(ValueError):
    """Raised when a requested path resolves outside the session root."""


def safe_resolve(root: Path, requested: str) -> Optional[Path]:
    """Resolve ``requested`` against ``root`` or refuse.

    Returns the resolved absolute path if it exists and is contained
    inside ``root`` (after following symlinks). Returns ``None`` if the
    path simply does not exist. Raises :class:`UnsafePathError` if the
    request tries to escape the root in any way — absolute paths,
    parent traversals, and symlinks pointing outside all qualify.
    """
    if requested.startswith("/") or requested.startswith("\\"):
        raise UnsafePathError(f"absolute path rejected: {requested!r}")

    root_abs = root.resolve(strict=True)
    candidate = (root_abs / requested).resolve(strict=False)

    try:
        candidate.relative_to(root_abs)
    except ValueError as exc:
        raise UnsafePathError(
            f"path escapes session root: {requested!r}"
        ) from exc

    if not candidate.exists():
        return None
    return candidate
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/unit/replay/test_handler.py -v`

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/syncfield/replay/_handler.py tests/unit/replay/test_handler.py
git commit -m "feat(replay): path-traversal-safe file resolution helper"
```

---

## Phase 2 — Python server

### Task 4: `server.py` — Starlette app + routes

**Files:**
- Create: `src/syncfield/replay/server.py`
- Create: `tests/unit/replay/test_server.py`

- [ ] **Step 1: Write the failing server tests**

Write `tests/unit/replay/test_server.py`:

```python
"""Tests for the Starlette replay server (no real network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from syncfield.replay.loader import load_session
from syncfield.replay.server import build_app


@pytest.fixture
def client_with_synthetic(synthetic_session: Path) -> TestClient:
    manifest = load_session(synthetic_session)
    app = build_app(manifest)
    return TestClient(app)


@pytest.fixture
def client_with_synced(synced_session: Path) -> TestClient:
    manifest = load_session(synced_session)
    app = build_app(manifest)
    return TestClient(app)


def test_get_session_returns_manifest_json(
    client_with_synthetic: TestClient,
) -> None:
    response = client_with_synthetic.get("/api/session")
    assert response.status_code == 200
    body = response.json()
    assert body["host_id"] == "test_rig"
    assert {s["id"] for s in body["streams"]} == {"cam_ego", "wrist_imu"}


def test_get_sync_report_404_when_missing(
    client_with_synthetic: TestClient,
) -> None:
    response = client_with_synthetic.get("/api/sync-report")
    assert response.status_code == 404


def test_get_sync_report_returns_json_when_present(
    client_with_synced: TestClient,
) -> None:
    response = client_with_synced.get("/api/sync-report")
    assert response.status_code == 200
    assert response.json()["streams"]["cam_ego"]["quality"] == "excellent"


def test_get_media_serves_video_bytes(
    client_with_synthetic: TestClient, synthetic_session: Path,
) -> None:
    response = client_with_synthetic.get("/media/cam_ego")
    assert response.status_code == 200
    expected = (synthetic_session / "cam_ego.mp4").read_bytes()
    assert response.content == expected


def test_get_media_supports_range_request(
    client_with_synthetic: TestClient,
) -> None:
    response = client_with_synthetic.get(
        "/media/cam_ego", headers={"Range": "bytes=0-15"},
    )
    assert response.status_code == 206
    assert "content-range" in {k.lower() for k in response.headers}
    assert len(response.content) == 16


def test_get_media_unknown_stream_returns_404(
    client_with_synthetic: TestClient,
) -> None:
    response = client_with_synthetic.get("/media/no_such_stream")
    assert response.status_code == 404


def test_get_data_serves_jsonl(
    client_with_synthetic: TestClient,
) -> None:
    response = client_with_synthetic.get("/data/wrist_imu.jsonl")
    assert response.status_code == 200
    assert b"channels" in response.content


def test_get_data_path_traversal_rejected(
    client_with_synthetic: TestClient,
) -> None:
    response = client_with_synthetic.get("/data/..%2Fetc%2Fpasswd")
    assert response.status_code in (400, 404)


def test_get_root_serves_index_html(
    client_with_synthetic: TestClient,
) -> None:
    response = client_with_synthetic.get("/")
    assert response.status_code == 200
    assert b"<html" in response.content.lower() or b"<!doctype" in response.content.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/replay/test_server.py -v`

Expected: All tests fail with `ModuleNotFoundError: No module named 'syncfield.replay.server'`.

- [ ] **Step 3: Add a placeholder index.html so the root route has something to serve**

Run:
```bash
mkdir -p src/syncfield/replay/static
cat > src/syncfield/replay/static/index.html <<'HTML'
<!doctype html>
<html><head><meta charset="utf-8"><title>SyncField Replay</title></head>
<body><div id="root">replay viewer placeholder</div></body></html>
HTML
```

- [ ] **Step 4: Implement `server.py`**

Write `src/syncfield/replay/server.py`:

```python
"""Local HTTP server for the replay viewer.

Serves a small JSON API plus the bundled SPA. Bound to ``127.0.0.1`` by
default — never bind a public interface, the routes assume a trusted
single origin.
"""

from __future__ import annotations

import logging
from importlib.resources import as_file, files
from pathlib import Path
from typing import Optional

import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from syncfield.replay._handler import UnsafePathError, safe_resolve
from syncfield.replay.loader import ReplayManifest

logger = logging.getLogger(__name__)


def _static_dir() -> Path:
    """Return the bundled static directory shipped inside the package."""
    pkg_root = files("syncfield.replay").joinpath("static")
    with as_file(pkg_root) as p:
        return Path(p)


def build_app(manifest: ReplayManifest) -> Starlette:
    """Construct a Starlette app bound to a single session manifest."""
    static_dir = _static_dir()
    streams_by_id = {s.id: s for s in manifest.streams}

    async def get_session(_request: Request) -> JSONResponse:
        return JSONResponse(manifest.to_json())

    async def get_sync_report(_request: Request) -> Response:
        if manifest.sync_report is None:
            return JSONResponse({"detail": "no sync report"}, status_code=404)
        return JSONResponse(manifest.sync_report)

    async def get_media(request: Request) -> Response:
        stream_id = request.path_params["stream_id"]
        stream = streams_by_id.get(stream_id)
        if stream is None or stream.media_path is None:
            raise HTTPException(status_code=404)
        return FileResponse(stream.media_path, media_type="video/mp4")

    async def get_data(request: Request) -> Response:
        filename = request.path_params["filename"]
        try:
            resolved = safe_resolve(manifest.session_dir, filename)
        except UnsafePathError:
            raise HTTPException(status_code=400)
        if resolved is None or not resolved.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(resolved)

    routes = [
        Route("/api/session", get_session),
        Route("/api/sync-report", get_sync_report),
        Route("/media/{stream_id}", get_media),
        Route("/data/{filename:path}", get_data),
        Mount(
            "/",
            app=StaticFiles(directory=str(static_dir), html=True),
            name="static",
        ),
    ]
    return Starlette(routes=routes)


class ReplayServer:
    """Wraps a uvicorn server bound to a single session.

    The instance owns its own ``uvicorn.Server`` so the caller can shut
    it down without touching the global event loop.
    """

    def __init__(
        self,
        manifest: ReplayManifest,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._manifest = manifest
        self._app = build_app(manifest)
        config = uvicorn.Config(
            self._app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)

    @property
    def url(self) -> str:
        host = self._server.config.host
        port = self._server.config.port
        return f"http://{host}:{port}/"

    def serve(self) -> None:
        """Run the server on the calling thread until shutdown."""
        self._server.run()

    def should_exit(self) -> None:
        self._server.should_exit = True
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/unit/replay/test_server.py -v`

Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add src/syncfield/replay/server.py src/syncfield/replay/static/index.html tests/unit/replay/test_server.py
git commit -m "feat(replay): Starlette server with /api/session, /api/sync-report, /media, /data routes"
```

---

### Task 5: Public `launch()` + smoke test

**Files:**
- Modify: `src/syncfield/replay/__init__.py`
- Create: `src/syncfield/replay/__main__.py`
- Create: `tests/unit/replay/test_launch_smoke.py`

- [ ] **Step 1: Write the failing smoke test**

Write `tests/unit/replay/test_launch_smoke.py`:

```python
"""End-to-end smoke test: launch() actually serves /api/session."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from syncfield.replay import launch
from syncfield.replay.loader import load_session
from syncfield.replay.server import ReplayServer


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_launch_starts_server_and_serves_session(
    synthetic_session: Path,
) -> None:
    port = _free_port()
    server_ref: list[ReplayServer] = []

    def _run() -> None:
        # We bypass launch() so the test can hold the ReplayServer
        # reference and shut it down deterministically.
        manifest = load_session(synthetic_session)
        server = ReplayServer(manifest, host="127.0.0.1", port=port)
        server_ref.append(server)
        server.serve()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/session", timeout=0.5)
            if r.status_code == 200:
                break
        except httpx.RequestError:
            time.sleep(0.05)
    else:
        pytest.fail("server never came up")

    body = r.json()
    assert body["host_id"] == "test_rig"

    server_ref[0].should_exit()
    thread.join(timeout=3.0)
    assert not thread.is_alive()


def test_launch_is_callable_with_open_browser_false(
    synthetic_session: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Just verify the public function exists and accepts the keyword."""
    # Patch ReplayServer.serve so we don't actually block.
    called = {"serve": False}

    def fake_serve(self):  # type: ignore[no-untyped-def]
        called["serve"] = True

    monkeypatch.setattr(
        "syncfield.replay.server.ReplayServer.serve", fake_serve
    )

    launch(synthetic_session, open_browser=False)
    assert called["serve"] is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/replay/test_launch_smoke.py -v`

Expected: Failures importing `launch` from `syncfield.replay`.

- [ ] **Step 3: Implement `launch()`**

Edit `src/syncfield/replay/__init__.py` — replace its body (keep the docstring and import guard) with:

```python
"""SyncField replay viewer — local browser-based session playback.

Open a previously recorded session in your default browser to verify
sync quality (per-stream offsets, Before/After comparison, sync report).

Usage::

    import syncfield as sf
    sf.replay.launch("./data/session_2026-04-09T14-49")

Requires the ``replay`` extra::

    pip install 'syncfield[replay]'
"""

from __future__ import annotations

try:
    import starlette  # noqa: F401
    import uvicorn  # noqa: F401
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "syncfield.replay requires the 'replay' extra. "
        "Install with `pip install 'syncfield[replay]'`."
    ) from exc

import logging
import threading
import time
import webbrowser
from pathlib import Path
from typing import Union

from syncfield.replay.loader import load_session
from syncfield.replay.server import ReplayServer

logger = logging.getLogger(__name__)

__all__ = ["launch"]


def launch(
    session_dir: Union[str, Path],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """Open a synced-session replay viewer in the default browser.

    Args:
        session_dir: Path to a session folder written by
            :class:`syncfield.SessionOrchestrator`.
        host: Bind interface. Defaults to localhost; do not change
            this without understanding that there is no auth layer.
        port: TCP port. ``0`` picks an ephemeral free port.
        open_browser: If True (default), open the user's default
            browser to the served URL once the server is ready.

    The function blocks the calling thread until the server stops.
    Press Ctrl+C to terminate.
    """
    manifest = load_session(Path(session_dir))
    server = ReplayServer(manifest, host=host, port=port)

    if open_browser:
        def _open_after_ready() -> None:
            time.sleep(0.2)
            try:
                webbrowser.open(server.url)
            except Exception:
                logger.warning("could not open browser", exc_info=True)

        threading.Thread(
            target=_open_after_ready,
            name="replay-browser-launcher",
            daemon=True,
        ).start()

    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("replay server interrupted, shutting down")
```

- [ ] **Step 4: Add a `__main__.py` entry point for dev mode**

Write `src/syncfield/replay/__main__.py`:

```python
"""``python -m syncfield.replay <session_dir>`` — convenience entry point.

Mostly used during frontend development with the Vite dev server's proxy
pointed at this Python process. Same arguments as :func:`launch`.
"""

from __future__ import annotations

import argparse
import sys

from syncfield.replay import launch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m syncfield.replay",
        description="Open a SyncField session in the local replay viewer.",
    )
    parser.add_argument("session_dir", help="Path to a recorded session folder")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--no-browser",
        dest="open_browser",
        action="store_false",
        help="Do not auto-open the browser (useful with vite dev server).",
    )
    args = parser.parse_args(argv)

    launch(
        args.session_dir,
        host=args.host,
        port=args.port,
        open_browser=args.open_browser,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the smoke tests**

Run: `pytest tests/unit/replay/test_launch_smoke.py -v`

Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add src/syncfield/replay/__init__.py src/syncfield/replay/__main__.py tests/unit/replay/test_launch_smoke.py
git commit -m "feat(replay): public sf.replay.launch() entry point + python -m wrapper"
```

---

### Task 6: Wire up `[replay]` extra in `pyproject.toml`

**Files:**
- Modify: `pyproject.toml:42-60`

- [ ] **Step 1: Add the `[replay]` extra and force-include the static dir**

Edit `pyproject.toml`. Find the `[project.optional-dependencies]` block and add a new `replay` entry after `viewer`:

```toml
# Local browser-based replay viewer (syncfield.replay). Starlette + uvicorn
# serve a pre-built React SPA from the package's static/ directory.
replay = [
    "starlette>=0.36",
    "uvicorn>=0.27",
]
```

Update the `all` group to include the new extras:

```toml
all = [
    "sounddevice>=0.4.6",
    "numpy>=1.21",
    "opencv-python>=4.5",
    "bleak>=0.21",
    "depthai>=3.0.0",
    "dearpygui>=2.0",
    "zeroconf>=0.130",
    "starlette>=0.36",
    "uvicorn>=0.27",
]
```

Add a force-include block right after `[tool.hatch.build.targets.wheel]` so the built wheel ships the static directory:

```toml
[tool.hatch.build.targets.wheel.force-include]
"src/syncfield/replay/static" = "syncfield/replay/static"
```

- [ ] **Step 2: Verify the project still builds**

Run: `python -m build --wheel --outdir /tmp/sf-wheel-check 2>&1 | tail -20`

(If `build` is not installed: `uv pip install build` first.)

Expected: Build succeeds and produces a `.whl`.

- [ ] **Step 3: Verify the wheel actually contains the static directory**

Run: `python -c "import zipfile; w = zipfile.ZipFile(sorted(__import__('glob').glob('/tmp/sf-wheel-check/syncfield-*.whl'))[-1]); print('\n'.join(n for n in w.namelist() if 'replay/static' in n))"`

Expected: At least `syncfield/replay/static/index.html` appears in the listing.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build(replay): add [replay] extra and force-include static/ in wheel"
```

---

## Phase 3 — Web SPA scaffold

### Task 7: Initialize the Vite + React + Tailwind project

**Files:**
- Create: `src/syncfield/replay/_web/` (entire Vite project)

- [ ] **Step 1: Scaffold the project with yarn**

Run:
```bash
cd src/syncfield/replay
mkdir _web
cd _web
yarn init -y
```

- [ ] **Step 2: Install runtime + dev dependencies**

Match egonaut/web's versions where possible:

```bash
yarn add react@^19 react-dom@^19 lucide-react@^1
yarn add -D vite@^8 @vitejs/plugin-react@^6 typescript@~5.9 \
  @types/react@^19 @types/react-dom@^19 \
  tailwindcss@^4.2 @tailwindcss/vite@^4.2 \
  vitest@^4 @testing-library/react@^16 @testing-library/jest-dom@^6 jsdom@^29
```

- [ ] **Step 3: Create `package.json` scripts**

Edit `_web/package.json` so its `scripts` section reads:

```json
"scripts": {
  "dev": "vite",
  "build": "tsc -b && vite build",
  "test": "vitest run",
  "test:watch": "vitest"
}
```

- [ ] **Step 4: Create `vite.config.ts`**

Write `src/syncfield/replay/_web/vite.config.ts`:

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

const PY_DEV_PORT = 8765;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": `http://127.0.0.1:${PY_DEV_PORT}`,
      "/media": `http://127.0.0.1:${PY_DEV_PORT}`,
      "/data": `http://127.0.0.1:${PY_DEV_PORT}`,
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
  },
});
```

- [ ] **Step 5: Create `tsconfig.json` and `tsconfig.node.json`**

Write `src/syncfield/replay/_web/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "useDefineForClassFields": true,
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": false,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "baseUrl": ".",
    "paths": { "@/*": ["src/*"] },
    "types": ["vitest/globals", "@testing-library/jest-dom"]
  },
  "include": ["src", "vitest.setup.ts"],
  "references": [{ "path": "./tsconfig.node.json" }]
}
```

Write `src/syncfield/replay/_web/tsconfig.node.json`:

```json
{
  "compilerOptions": {
    "composite": true,
    "skipLibCheck": true,
    "module": "ESNext",
    "moduleResolution": "bundler",
    "allowSyntheticDefaultImports": true,
    "strict": true
  },
  "include": ["vite.config.ts"]
}
```

- [ ] **Step 6: Create `index.html`, `src/main.tsx`, `src/App.tsx`, `src/index.css`, `vitest.setup.ts`**

Write `src/syncfield/replay/_web/index.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>SyncField Replay</title>
  </head>
  <body class="bg-[#FAF8F6]">
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

Write `src/syncfield/replay/_web/src/main.tsx`:

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

Write `src/syncfield/replay/_web/src/index.css`:

```css
@import "tailwindcss";

:root {
  --bg-app: #faf8f6;
  --text-primary: #18181b;
  --text-secondary: #52525b;
  --text-muted: #a1a1aa;
}

html, body, #root {
  height: 100%;
}
```

Write `src/syncfield/replay/_web/src/App.tsx`:

```tsx
export default function App() {
  return (
    <div className="flex h-full items-center justify-center text-zinc-500">
      SyncField Replay — bootstrapping
    </div>
  );
}
```

Write `src/syncfield/replay/_web/vitest.setup.ts`:

```ts
import "@testing-library/jest-dom";
```

- [ ] **Step 7: Add a `.gitignore` for the web project**

Write `src/syncfield/replay/_web/.gitignore`:

```
node_modules
dist
*.log
.DS_Store
```

(Note: `dist` would only appear if someone runs `vite build` without `outDir` override. Our config sends output to `../static`, so this is just defensive.)

- [ ] **Step 8: Verify the Vite project builds**

Run:
```bash
cd src/syncfield/replay/_web
yarn build
```

Expected: Build succeeds. Output appears at `src/syncfield/replay/static/index.html` (overwriting the placeholder from Task 4) plus an `assets/` directory.

- [ ] **Step 9: Commit**

```bash
cd ../../../..
git add src/syncfield/replay/_web src/syncfield/replay/static
git commit -m "feat(replay): scaffold _web/ Vite+React+Tailwind project, build to static/"
```

---

### Task 8: Types + `useReplaySession` hook

**Files:**
- Create: `src/syncfield/replay/_web/src/types.ts`
- Create: `src/syncfield/replay/_web/src/hooks/useReplaySession.ts`
- Create: `src/syncfield/replay/_web/src/hooks/__tests__/useReplaySession.test.tsx`

- [ ] **Step 1: Define the shared TypeScript types**

Write `src/syncfield/replay/_web/src/types.ts`:

```ts
export type StreamKind = "video" | "sensor" | "custom";

export interface ReplayStream {
  id: string;
  kind: StreamKind;
  media_url: string | null;
  data_url: string | null;
  frame_count: number;
}

export interface SyncPoint {
  monotonic_ns?: number;
  wall_clock_ns?: number;
  iso_datetime?: string;
  chirp_start_ns?: number;
  chirp_stop_ns?: number;
}

export interface SessionManifest {
  host_id: string;
  sync_point: SyncPoint;
  has_frame_map: boolean;
  streams: ReplayStream[];
}

export type SyncQuality = "excellent" | "good" | "fair" | "poor";

export interface SyncStreamResult {
  offset_seconds: number;
  confidence: number;
  quality: SyncQuality;
}

export interface SyncReport {
  streams: Record<string, SyncStreamResult>;
}
```

- [ ] **Step 2: Write the failing hook test**

Write `src/syncfield/replay/_web/src/hooks/__tests__/useReplaySession.test.tsx`:

```tsx
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useReplaySession } from "../useReplaySession";

const SESSION_FIXTURE = {
  host_id: "test_rig",
  sync_point: {},
  has_frame_map: false,
  streams: [
    { id: "cam_ego", kind: "video", media_url: "/media/cam_ego", data_url: null, frame_count: 60 },
  ],
};

const REPORT_FIXTURE = {
  streams: {
    cam_ego: { offset_seconds: 0.012, confidence: 0.97, quality: "excellent" },
  },
};

describe("useReplaySession", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/session")) {
          return new Response(JSON.stringify(SESSION_FIXTURE), { status: 200 });
        }
        if (url.endsWith("/api/sync-report")) {
          return new Response(JSON.stringify(REPORT_FIXTURE), { status: 200 });
        }
        return new Response("not found", { status: 404 });
      }),
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads the session manifest and sync report", async () => {
    const { result } = renderHook(() => useReplaySession());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.session?.host_id).toBe("test_rig");
    expect(result.current.syncReport?.streams.cam_ego.quality).toBe("excellent");
  });

  it("treats a 404 sync report as null", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/session")) {
          return new Response(JSON.stringify(SESSION_FIXTURE), { status: 200 });
        }
        return new Response("not found", { status: 404 });
      }),
    );

    const { result } = renderHook(() => useReplaySession());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.session?.host_id).toBe("test_rig");
    expect(result.current.syncReport).toBeNull();
  });
});
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd src/syncfield/replay/_web && yarn test`

Expected: Tests fail because `useReplaySession` does not exist.

- [ ] **Step 4: Implement the hook**

Write `src/syncfield/replay/_web/src/hooks/useReplaySession.ts`:

```ts
import { useEffect, useState } from "react";
import type { SessionManifest, SyncReport } from "../types";

export interface ReplaySessionState {
  session: SessionManifest | null;
  syncReport: SyncReport | null;
  loading: boolean;
  error: string | null;
}

export function useReplaySession(): ReplaySessionState {
  const [session, setSession] = useState<SessionManifest | null>(null);
  const [syncReport, setSyncReport] = useState<SyncReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [s, r] = await Promise.all([
          fetch("/api/session"),
          fetch("/api/sync-report"),
        ]);
        if (!s.ok) throw new Error(`session fetch failed: ${s.status}`);
        const sessionJson = (await s.json()) as SessionManifest;
        const reportJson = r.ok ? ((await r.json()) as SyncReport) : null;
        if (!cancelled) {
          setSession(sessionJson);
          setSyncReport(reportJson);
        }
      } catch (err) {
        if (!cancelled) setError(String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return { session, syncReport, loading, error };
}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `yarn test`

Expected: Tests pass.

- [ ] **Step 6: Commit**

```bash
cd ../../../../..
git add src/syncfield/replay/_web/src/types.ts src/syncfield/replay/_web/src/hooks
git commit -m "feat(replay/web): SessionManifest types + useReplaySession data hook"
```

---

### Task 9: `useBeforeAfter` hook — offset math

**Files:**
- Create: `src/syncfield/replay/_web/src/hooks/useBeforeAfter.ts`
- Create: `src/syncfield/replay/_web/src/hooks/__tests__/useBeforeAfter.test.ts`

- [ ] **Step 1: Write the failing tests for the offset math**

Write `src/syncfield/replay/_web/src/hooks/__tests__/useBeforeAfter.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { computeStreamTime } from "../useBeforeAfter";

describe("computeStreamTime", () => {
  it("returns the master time unchanged in 'before' mode", () => {
    expect(computeStreamTime(5.0, 0.3, "before")).toBe(5.0);
  });

  it("subtracts the offset in 'after' mode", () => {
    expect(computeStreamTime(5.0, 0.3, "after")).toBeCloseTo(4.7, 5);
  });

  it("clamps negative results to 0", () => {
    expect(computeStreamTime(0.1, 0.5, "after")).toBe(0);
  });

  it("treats a missing offset as 0", () => {
    expect(computeStreamTime(5.0, undefined, "after")).toBe(5.0);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `yarn test useBeforeAfter`

Expected: Tests fail with module not found.

- [ ] **Step 3: Implement the hook**

Write `src/syncfield/replay/_web/src/hooks/useBeforeAfter.ts`:

```ts
import { useCallback, useEffect, useState } from "react";
import type { SyncReport } from "../types";

export type SyncMode = "before" | "after";

/** Pure helper — keeps the math testable in isolation. */
export function computeStreamTime(
  masterTime: number,
  offsetSeconds: number | undefined,
  mode: SyncMode,
): number {
  if (mode === "before") return masterTime;
  const offset = offsetSeconds ?? 0;
  return Math.max(0, masterTime - offset);
}

export interface BeforeAfterState {
  mode: SyncMode;
  toggle: () => void;
  setMode: (next: SyncMode) => void;
  offsetFor: (streamId: string) => number | undefined;
  hasReport: boolean;
}

export function useBeforeAfter(report: SyncReport | null): BeforeAfterState {
  const hasReport = report !== null;
  const [mode, setMode] = useState<SyncMode>(hasReport ? "after" : "before");

  // If a report shows up after initial render (slow fetch), default to after.
  useEffect(() => {
    if (hasReport) setMode("after");
  }, [hasReport]);

  const toggle = useCallback(() => {
    if (!hasReport) return;
    setMode((m) => (m === "after" ? "before" : "after"));
  }, [hasReport]);

  const offsetFor = useCallback(
    (streamId: string): number | undefined =>
      report?.streams[streamId]?.offset_seconds,
    [report],
  );

  // Keyboard shortcut: B
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      if (e.code === "KeyB") {
        e.preventDefault();
        toggle();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [toggle]);

  return { mode, toggle, setMode, offsetFor, hasReport };
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `yarn test useBeforeAfter`

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd ../../../../..
git add src/syncfield/replay/_web/src/hooks/useBeforeAfter.ts src/syncfield/replay/_web/src/hooks/__tests__/useBeforeAfter.test.ts
git commit -m "feat(replay/web): useBeforeAfter hook with pure offset math + B shortcut"
```

---

## Phase 4 — Web SPA UI

### Task 10: `SyncReportPanel` component

**Files:**
- Create: `src/syncfield/replay/_web/src/components/SyncReportPanel.tsx`
- Create: `src/syncfield/replay/_web/src/components/__tests__/SyncReportPanel.test.tsx`
- Create: `src/syncfield/replay/_web/src/lib/quality.ts`

- [ ] **Step 1: Extract the quality color helper from egonaut**

Write `src/syncfield/replay/_web/src/lib/quality.ts`:

```ts
import type { SyncQuality } from "../types";

export function qualityColor(quality: SyncQuality | string): string {
  switch (quality) {
    case "excellent":
      return "bg-green-100 text-green-700";
    case "good":
      return "bg-blue-100 text-blue-700";
    case "fair":
      return "bg-amber-100 text-amber-700";
    case "poor":
      return "bg-red-100 text-red-700";
    default:
      return "bg-zinc-100 text-zinc-600";
  }
}
```

- [ ] **Step 2: Write the failing component test**

Write `src/syncfield/replay/_web/src/components/__tests__/SyncReportPanel.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import SyncReportPanel from "../SyncReportPanel";
import type { SyncReport } from "../../types";

const REPORT: SyncReport = {
  streams: {
    cam_ego: { offset_seconds: 0.012, confidence: 0.97, quality: "excellent" },
    wrist_imu: { offset_seconds: -0.034, confidence: 0.81, quality: "good" },
  },
};

describe("SyncReportPanel", () => {
  it("renders one card per stream with offset and quality", () => {
    render(<SyncReportPanel report={REPORT} />);
    expect(screen.getByText("cam_ego")).toBeInTheDocument();
    expect(screen.getByText("+0.012s")).toBeInTheDocument();
    expect(screen.getByText("wrist_imu")).toBeInTheDocument();
    expect(screen.getByText("-0.034s")).toBeInTheDocument();
    expect(screen.getByText("excellent")).toBeInTheDocument();
    expect(screen.getByText("good")).toBeInTheDocument();
  });

  it("renders an empty placeholder when no report is provided", () => {
    render(<SyncReportPanel report={null} />);
    expect(screen.getByText(/sync not run/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `yarn test SyncReportPanel`

Expected: Tests fail because `SyncReportPanel` does not exist.

- [ ] **Step 4: Implement `SyncReportPanel`**

Write `src/syncfield/replay/_web/src/components/SyncReportPanel.tsx`:

```tsx
import { CheckCircle2, AlertCircle } from "lucide-react";
import type { SyncReport, SyncStreamResult } from "../types";
import { qualityColor } from "../lib/quality";

function formatOffset(seconds: number): string {
  const sign = seconds >= 0 ? "+" : "";
  return `${sign}${seconds.toFixed(3)}s`;
}

function StreamCard({
  streamId,
  result,
}: {
  streamId: string;
  result: SyncStreamResult;
}) {
  const confidencePct = Math.round(result.confidence * 100);
  return (
    <div className="rounded-xl border border-zinc-200/80 bg-white px-4 py-3 min-w-[200px]">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[11px] font-mono text-zinc-600 truncate">
          {streamId}
        </span>
        <span
          className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${qualityColor(result.quality)}`}
        >
          {result.quality}
        </span>
      </div>
      <div className="font-mono text-sm text-zinc-800 tabular-nums">
        {formatOffset(result.offset_seconds)}
      </div>
      <div className="mt-2 flex items-center gap-2">
        <div className="h-1 flex-1 overflow-hidden rounded-full bg-zinc-100">
          <div
            className="h-full rounded-full bg-cyan-500"
            style={{ width: `${confidencePct}%` }}
          />
        </div>
        <span className="text-[10px] text-zinc-400 tabular-nums">
          {confidencePct}%
        </span>
      </div>
    </div>
  );
}

export default function SyncReportPanel({
  report,
}: {
  report: SyncReport | null;
}) {
  if (!report) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-amber-200 bg-amber-50/40 px-4 py-3">
        <AlertCircle size={16} className="text-amber-600" />
        <span className="text-xs text-amber-700">
          Sync not run yet — only Before view available
        </span>
      </div>
    );
  }

  const entries = Object.entries(report.streams);

  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <CheckCircle2 size={14} className="text-teal-600" />
        <span className="text-[11px] uppercase tracking-wider text-zinc-500">
          Sync report
        </span>
      </div>
      <div className="flex flex-wrap gap-2">
        {entries.map(([id, result]) => (
          <StreamCard key={id} streamId={id} result={result} />
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `yarn test SyncReportPanel`

Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
cd ../../../../..
git add src/syncfield/replay/_web/src/components src/syncfield/replay/_web/src/lib
git commit -m "feat(replay/web): SyncReportPanel with per-stream offset/confidence/quality cards"
```

---

### Task 11: `BeforeAfterToggle` component

**Files:**
- Create: `src/syncfield/replay/_web/src/components/BeforeAfterToggle.tsx`
- Create: `src/syncfield/replay/_web/src/components/__tests__/BeforeAfterToggle.test.tsx`

- [ ] **Step 1: Write the failing test**

Write `src/syncfield/replay/_web/src/components/__tests__/BeforeAfterToggle.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import BeforeAfterToggle from "../BeforeAfterToggle";

describe("BeforeAfterToggle", () => {
  it("highlights the active mode", () => {
    render(
      <BeforeAfterToggle mode="after" disabled={false} onChange={() => {}} />,
    );
    const after = screen.getByRole("button", { name: /after/i });
    expect(after.className).toMatch(/bg-zinc-900/);
  });

  it("calls onChange when the inactive button is clicked", () => {
    const onChange = vi.fn();
    render(
      <BeforeAfterToggle mode="after" disabled={false} onChange={onChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /before/i }));
    expect(onChange).toHaveBeenCalledWith("before");
  });

  it("disables both buttons when disabled prop is true", () => {
    render(
      <BeforeAfterToggle mode="before" disabled onChange={() => {}} />,
    );
    expect(screen.getByRole("button", { name: /before/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /after/i })).toBeDisabled();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `yarn test BeforeAfterToggle`

Expected: Tests fail with module not found.

- [ ] **Step 3: Implement the component**

Write `src/syncfield/replay/_web/src/components/BeforeAfterToggle.tsx`:

```tsx
import type { SyncMode } from "../hooks/useBeforeAfter";

interface Props {
  mode: SyncMode;
  disabled: boolean;
  onChange: (next: SyncMode) => void;
}

const BASE =
  "px-4 py-1.5 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50";
const ACTIVE = "bg-zinc-900 text-white";
const INACTIVE = "text-zinc-500 hover:text-zinc-800";

export default function BeforeAfterToggle({ mode, disabled, onChange }: Props) {
  return (
    <div className="inline-flex rounded-full border border-zinc-200 bg-white p-0.5">
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange("before")}
        className={`${BASE} rounded-full ${mode === "before" ? ACTIVE : INACTIVE}`}
      >
        Before
      </button>
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange("after")}
        className={`${BASE} rounded-full ${mode === "after" ? ACTIVE : INACTIVE}`}
      >
        After
      </button>
    </div>
  );
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `yarn test BeforeAfterToggle`

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
cd ../../../../..
git add src/syncfield/replay/_web/src/components/BeforeAfterToggle.tsx src/syncfield/replay/_web/src/components/__tests__/BeforeAfterToggle.test.tsx
git commit -m "feat(replay/web): BeforeAfterToggle segmented control"
```

---

### Task 12: `VideoArea` — multi-stream offset-aware playback

**Files:**
- Create: `src/syncfield/replay/_web/src/components/VideoArea.tsx`
- Create: `src/syncfield/replay/_web/src/components/__tests__/VideoArea.test.tsx`

This is the largest UI piece. We port egonaut's `VideoArea` while:
1. dropping signed-URL fetching (URLs come pre-formed from the API),
2. wiring per-video offset application from `useBeforeAfter`,
3. dropping any tactile/contact-overlay coupling that depends on data we no longer have,
4. keeping the playhead/scrub/keyboard controls.

- [ ] **Step 1: Write the smoke test**

Write `src/syncfield/replay/_web/src/components/__tests__/VideoArea.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import VideoArea from "../VideoArea";
import type { ReplayStream } from "../../types";

const STREAMS: ReplayStream[] = [
  { id: "cam_ego", kind: "video", media_url: "/media/cam_ego", data_url: null, frame_count: 60 },
  { id: "wrist_left", kind: "video", media_url: "/media/wrist_left", data_url: null, frame_count: 60 },
];

// jsdom does not implement HTMLMediaElement playback methods.
beforeEach(() => {
  Object.defineProperty(HTMLMediaElement.prototype, "play", {
    configurable: true,
    value: vi.fn().mockResolvedValue(undefined),
  });
  Object.defineProperty(HTMLMediaElement.prototype, "pause", {
    configurable: true,
    value: vi.fn(),
  });
});

describe("VideoArea", () => {
  it("renders one <video> per video stream", () => {
    render(
      <VideoArea
        streams={STREAMS}
        mode="after"
        offsetFor={() => 0}
        masterTime={0}
        isPlaying={false}
        seekVersion={0}
      />,
    );
    const videos = screen.getAllByTestId("replay-video");
    expect(videos).toHaveLength(2);
    expect(videos[0]).toHaveAttribute("src", "/media/cam_ego");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `yarn test VideoArea`

Expected: Tests fail because `VideoArea` does not exist.

- [ ] **Step 3: Implement `VideoArea`**

Write `src/syncfield/replay/_web/src/components/VideoArea.tsx`:

```tsx
import { useEffect, useRef } from "react";
import type { ReplayStream } from "../types";
import type { SyncMode } from "../hooks/useBeforeAfter";
import { computeStreamTime } from "../hooks/useBeforeAfter";

interface Props {
  streams: ReplayStream[];
  mode: SyncMode;
  offsetFor: (streamId: string) => number | undefined;
  masterTime: number;
  isPlaying: boolean;
  seekVersion: number;
  onTimeUpdate?: (time: number) => void;
  onDurationChange?: (duration: number) => void;
}

const DRIFT_TOLERANCE = 0.05; // seconds

export default function VideoArea({
  streams,
  mode,
  offsetFor,
  masterTime,
  isPlaying,
  seekVersion,
  onTimeUpdate,
  onDurationChange,
}: Props) {
  const videoStreams = streams.filter((s) => s.kind === "video" && s.media_url);
  const videoRefs = useRef<Map<string, HTMLVideoElement>>(new Map());

  // Apply seek when version bumps OR mode flips OR offsets shift.
  useEffect(() => {
    for (const s of videoStreams) {
      const el = videoRefs.current.get(s.id);
      if (!el) continue;
      const target = computeStreamTime(masterTime, offsetFor(s.id), mode);
      if (Math.abs(el.currentTime - target) > DRIFT_TOLERANCE) {
        el.currentTime = target;
      }
    }
  }, [seekVersion, mode, masterTime, videoStreams, offsetFor]);

  // Play / pause sync
  useEffect(() => {
    for (const s of videoStreams) {
      const el = videoRefs.current.get(s.id);
      if (!el) continue;
      if (isPlaying) {
        el.play().catch(() => {});
      } else {
        el.pause();
      }
    }
  }, [isPlaying, videoStreams]);

  if (videoStreams.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-zinc-400 text-sm">
        No video streams in this session
      </div>
    );
  }

  // First video drives the master time. Other videos follow via seek effect.
  const heroId = videoStreams[0].id;

  return (
    <div className="grid h-full w-full gap-2 p-2 grid-cols-1 md:grid-cols-2 bg-black">
      {videoStreams.map((s) => (
        <div key={s.id} className="relative bg-black">
          <video
            data-testid="replay-video"
            ref={(el) => {
              if (el) videoRefs.current.set(s.id, el);
              else videoRefs.current.delete(s.id);
            }}
            src={s.media_url ?? undefined}
            preload="auto"
            className="h-full w-full object-contain"
            onTimeUpdate={
              s.id === heroId && onTimeUpdate
                ? (e) => onTimeUpdate(e.currentTarget.currentTime)
                : undefined
            }
            onLoadedMetadata={
              s.id === heroId && onDurationChange
                ? (e) => onDurationChange(e.currentTarget.duration)
                : undefined
            }
          />
          <div className="absolute left-2 top-2 rounded bg-black/60 px-2 py-0.5 font-mono text-[10px] text-white">
            {s.id}
          </div>
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `yarn test VideoArea`

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
cd ../../../../..
git add src/syncfield/replay/_web/src/components/VideoArea.tsx src/syncfield/replay/_web/src/components/__tests__/VideoArea.test.tsx
git commit -m "feat(replay/web): VideoArea with per-stream offset application"
```

---

### Task 13: `SensorChartPanel` — minimal multi-stream charts

**Files:**
- Create: `src/syncfield/replay/_web/src/lib/sensorParser.ts`
- Create: `src/syncfield/replay/_web/src/hooks/useSensorData.ts`
- Create: `src/syncfield/replay/_web/src/components/SensorChartPanel.tsx`
- Create: `src/syncfield/replay/_web/src/components/__tests__/SensorChartPanel.test.tsx`

- [ ] **Step 1: Add a tiny JSONL sensor parser**

Write `src/syncfield/replay/_web/src/lib/sensorParser.ts`:

```ts
export interface SensorSample {
  t_ns: number;
  channels: Record<string, number>;
}

/** Parse a JSONL response body into typed sensor samples. */
export function parseSensorJsonl(text: string): SensorSample[] {
  const out: SensorSample[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const obj = JSON.parse(trimmed);
      if (typeof obj.t_ns === "number" && typeof obj.channels === "object") {
        out.push({ t_ns: obj.t_ns, channels: obj.channels });
      }
    } catch {
      // skip malformed lines
    }
  }
  return out;
}
```

- [ ] **Step 2: Add a sensor-fetch hook**

Write `src/syncfield/replay/_web/src/hooks/useSensorData.ts`:

```ts
import { useEffect, useState } from "react";
import { parseSensorJsonl, type SensorSample } from "../lib/sensorParser";
import type { ReplayStream } from "../types";

export interface SensorStreamData {
  id: string;
  samples: SensorSample[];
  channelNames: string[];
}

export function useSensorData(streams: ReplayStream[]): SensorStreamData[] {
  const [data, setData] = useState<SensorStreamData[]>([]);

  useEffect(() => {
    let cancelled = false;
    const sensorStreams = streams.filter(
      (s) => s.kind === "sensor" && s.data_url,
    );
    Promise.all(
      sensorStreams.map(async (s) => {
        const r = await fetch(s.data_url!);
        if (!r.ok) return null;
        const samples = parseSensorJsonl(await r.text());
        const channelNames =
          samples.length > 0 ? Object.keys(samples[0].channels) : [];
        return { id: s.id, samples, channelNames };
      }),
    ).then((results) => {
      if (cancelled) return;
      setData(results.filter((r): r is SensorStreamData => r !== null));
    });
    return () => {
      cancelled = true;
    };
  }, [streams]);

  return data;
}
```

- [ ] **Step 3: Write the failing chart test**

Write `src/syncfield/replay/_web/src/components/__tests__/SensorChartPanel.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import SensorChartPanel from "../SensorChartPanel";
import type { SensorStreamData } from "../../hooks/useSensorData";

const SENSORS: SensorStreamData[] = [
  {
    id: "wrist_imu",
    channelNames: ["ax", "ay"],
    samples: [
      { t_ns: 0, channels: { ax: 0.1, ay: -0.2 } },
      { t_ns: 1_000_000, channels: { ax: 0.2, ay: -0.3 } },
      { t_ns: 2_000_000, channels: { ax: 0.4, ay: -0.1 } },
    ],
  },
];

describe("SensorChartPanel", () => {
  it("renders one chart group per sensor stream", () => {
    render(
      <SensorChartPanel sensors={SENSORS} masterTime={0} duration={1} />,
    );
    expect(screen.getByText("wrist_imu")).toBeInTheDocument();
    // One <svg> per channel
    const svgs = document.querySelectorAll("svg.sensor-chart");
    expect(svgs.length).toBe(2);
  });

  it("renders empty state when no sensors", () => {
    render(<SensorChartPanel sensors={[]} masterTime={0} duration={1} />);
    expect(screen.getByText(/no sensor streams/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `yarn test SensorChartPanel`

Expected: Tests fail with module not found.

- [ ] **Step 5: Implement `SensorChartPanel`**

Write `src/syncfield/replay/_web/src/components/SensorChartPanel.tsx`:

```tsx
import { useMemo } from "react";
import type { SensorStreamData } from "../hooks/useSensorData";

interface Props {
  sensors: SensorStreamData[];
  masterTime: number;     // seconds
  duration: number;       // seconds
}

const CHART_W = 240;
const CHART_H = 56;
const PAD_X = 4;
const PAD_Y = 6;

interface ChannelPath {
  name: string;
  path: string;
  min: number;
  max: number;
}

function buildChannelPath(
  samples: SensorStreamData["samples"],
  channel: string,
): ChannelPath {
  if (samples.length === 0) return { name: channel, path: "", min: 0, max: 0 };
  const t0 = samples[0].t_ns;
  const tEnd = samples[samples.length - 1].t_ns;
  const span = Math.max(1, tEnd - t0);

  let min = Infinity;
  let max = -Infinity;
  for (const s of samples) {
    const v = s.channels[channel];
    if (typeof v !== "number") continue;
    if (v < min) min = v;
    if (v > max) max = v;
  }
  if (min === Infinity) return { name: channel, path: "", min: 0, max: 0 };
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const yRange = max - min;

  let d = "";
  for (let i = 0; i < samples.length; i++) {
    const s = samples[i];
    const v = s.channels[channel];
    if (typeof v !== "number") continue;
    const x = PAD_X + ((s.t_ns - t0) / span) * (CHART_W - 2 * PAD_X);
    const y = PAD_Y + (1 - (v - min) / yRange) * (CHART_H - 2 * PAD_Y);
    d += i === 0 ? `M${x.toFixed(1)},${y.toFixed(1)}` : `L${x.toFixed(1)},${y.toFixed(1)}`;
  }
  return { name: channel, path: d, min, max };
}

function StreamGroup({
  stream,
  playheadX,
}: {
  stream: SensorStreamData;
  playheadX: number;
}) {
  const channelPaths = useMemo(
    () => stream.channelNames.map((c) => buildChannelPath(stream.samples, c)),
    [stream],
  );

  return (
    <div className="mb-4">
      <div className="mb-1 flex items-center justify-between">
        <span className="font-mono text-[11px] text-zinc-600">{stream.id}</span>
        <span className="text-[10px] text-zinc-400">
          {stream.samples.length} samples
        </span>
      </div>
      <div className="space-y-1">
        {channelPaths.map((cp) => (
          <div key={cp.name} className="flex items-center gap-2">
            <span className="w-10 text-right font-mono text-[10px] text-zinc-400">
              {cp.name}
            </span>
            <svg
              className="sensor-chart"
              width={CHART_W}
              height={CHART_H}
              viewBox={`0 0 ${CHART_W} ${CHART_H}`}
            >
              <path
                d={cp.path}
                fill="none"
                stroke="#0891b2"
                strokeWidth={1}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
              {playheadX !== null && (
                <line
                  x1={playheadX}
                  x2={playheadX}
                  y1={0}
                  y2={CHART_H}
                  stroke="#a1a1aa"
                  strokeWidth={0.5}
                  strokeDasharray="2,2"
                />
              )}
            </svg>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function SensorChartPanel({
  sensors,
  masterTime,
  duration,
}: Props) {
  if (sensors.length === 0) {
    return (
      <div className="p-4 text-xs text-zinc-400">
        No sensor streams in this session
      </div>
    );
  }

  const playheadX =
    duration > 0
      ? PAD_X + (masterTime / duration) * (CHART_W - 2 * PAD_X)
      : PAD_X;

  return (
    <div className="h-full overflow-y-auto p-3">
      {sensors.map((s) => (
        <StreamGroup key={s.id} stream={s} playheadX={playheadX} />
      ))}
    </div>
  );
}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `yarn test SensorChartPanel`

Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
cd ../../../../..
git add src/syncfield/replay/_web/src/lib/sensorParser.ts src/syncfield/replay/_web/src/hooks/useSensorData.ts src/syncfield/replay/_web/src/components/SensorChartPanel.tsx src/syncfield/replay/_web/src/components/__tests__/SensorChartPanel.test.tsx
git commit -m "feat(replay/web): minimal multi-channel SensorChartPanel + SVG line charts"
```

---

### Task 14: `App.tsx` — wire everything together

**Files:**
- Modify: `src/syncfield/replay/_web/src/App.tsx` (full rewrite)
- Modify: `src/syncfield/replay/_web/src/index.css` (add small custom utilities if needed)

- [ ] **Step 1: Rewrite `App.tsx` as the full replay shell**

Write `src/syncfield/replay/_web/src/App.tsx`:

```tsx
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import VideoArea from "./components/VideoArea";
import SyncReportPanel from "./components/SyncReportPanel";
import BeforeAfterToggle from "./components/BeforeAfterToggle";
import SensorChartPanel from "./components/SensorChartPanel";
import { useReplaySession } from "./hooks/useReplaySession";
import { useBeforeAfter } from "./hooks/useBeforeAfter";
import { useSensorData } from "./hooks/useSensorData";

function formatTime(t: number): string {
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  const ms = Math.floor((t % 1) * 1000);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(ms).padStart(3, "0")}`;
}

export default function App() {
  const { session, syncReport, loading, error } = useReplaySession();
  const beforeAfter = useBeforeAfter(syncReport);
  const sensors = useSensorData(session?.streams ?? []);

  const [masterTime, setMasterTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [seekVersion, setSeekVersion] = useState(0);
  const seekTargetTimeRef = useRef(0);

  const seekTo = useCallback(
    (t: number) => {
      const clamped = Math.max(0, Math.min(t, duration || t));
      seekTargetTimeRef.current = clamped;
      setMasterTime(clamped);
      setSeekVersion((v) => v + 1);
    },
    [duration],
  );

  const togglePlay = useCallback(() => setIsPlaying((p) => !p), []);

  // Keyboard: Space play, ← / → 5s seek
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      switch (e.code) {
        case "Space":
          e.preventDefault();
          togglePlay();
          break;
        case "ArrowLeft":
          e.preventDefault();
          seekTo(masterTime - 5);
          break;
        case "ArrowRight":
          e.preventDefault();
          seekTo(masterTime + 5);
          break;
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [masterTime, seekTo, togglePlay]);

  const hasSensors = sensors.length > 0;

  const sessionContent = useMemo(() => {
    if (loading) {
      return (
        <div className="flex h-full items-center justify-center text-zinc-400 text-sm">
          Loading session…
        </div>
      );
    }
    if (error || !session) {
      return (
        <div className="flex h-full items-center justify-center text-red-500 text-sm">
          {error ?? "Session not found"}
        </div>
      );
    }
    return (
      <div className="flex flex-col h-full">
        {/* Header */}
        <div className="flex items-center gap-4 border-b border-zinc-200/60 bg-white px-5 py-3">
          <div>
            <div className="text-sm font-semibold text-zinc-800">SyncField Replay</div>
            <div className="font-mono text-[11px] text-zinc-500">{session.host_id}</div>
          </div>
          <div className="flex-1 flex justify-center">
            <BeforeAfterToggle
              mode={beforeAfter.mode}
              disabled={!beforeAfter.hasReport}
              onChange={beforeAfter.setMode}
            />
          </div>
          <div className="min-w-[260px]">
            <SyncReportPanel report={syncReport} />
          </div>
        </div>

        {/* Body: video left, sensors right */}
        <div className="flex flex-1 min-h-0">
          <div className="flex-[7] min-w-0">
            <VideoArea
              streams={session.streams}
              mode={beforeAfter.mode}
              offsetFor={beforeAfter.offsetFor}
              masterTime={masterTime}
              isPlaying={isPlaying}
              seekVersion={seekVersion}
              onTimeUpdate={(t) => {
                // ignore small updates while we're actively seeking
                if (Math.abs(t - seekTargetTimeRef.current) < 0.5) {
                  setMasterTime(t);
                }
              }}
              onDurationChange={setDuration}
            />
          </div>
          {hasSensors && (
            <div className="flex-[3] min-w-[300px] max-w-[480px] border-l border-zinc-200/60 bg-white">
              <SensorChartPanel
                sensors={sensors}
                masterTime={masterTime}
                duration={duration}
              />
            </div>
          )}
        </div>

        {/* Playback controls */}
        <div className="flex items-center gap-4 border-t border-zinc-200/60 bg-white px-4 py-2">
          <button
            type="button"
            onClick={togglePlay}
            className="rounded-md px-3 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-100"
          >
            {isPlaying ? "Pause" : "Play"}
          </button>
          <span className="font-mono text-xs text-zinc-500 tabular-nums">
            {formatTime(masterTime)} / {formatTime(duration)}
          </span>
          <input
            type="range"
            min={0}
            max={duration || 0}
            step={0.01}
            value={masterTime}
            onChange={(e) => seekTo(parseFloat(e.target.value))}
            className="flex-1 accent-zinc-700"
          />
        </div>
      </div>
    );
  }, [
    loading,
    error,
    session,
    syncReport,
    beforeAfter,
    masterTime,
    duration,
    isPlaying,
    seekVersion,
    seekTo,
    togglePlay,
    sensors,
    hasSensors,
  ]);

  return (
    <div className="h-full bg-[#FAF8F6] text-zinc-800">{sessionContent}</div>
  );
}
```

- [ ] **Step 2: Verify the tests still pass and the build still works**

Run:
```bash
yarn test
yarn build
```

Expected: All vitest tests pass, build succeeds.

- [ ] **Step 3: Commit**

```bash
cd ../../../../..
git add src/syncfield/replay/_web/src/App.tsx
git commit -m "feat(replay/web): wire App.tsx — header, VideoArea, SensorChartPanel, controls"
```

---

## Phase 5 — Build, ship, document

### Task 15: Commit the built `static/` bundle

**Files:**
- Modify: `src/syncfield/replay/static/` (replace placeholder with built artifacts)

- [ ] **Step 1: Run a clean production build**

Run:
```bash
cd src/syncfield/replay/_web
yarn build
```

Expected: `vite v8 building for production...` then a build summary. Output appears under `src/syncfield/replay/static/`.

- [ ] **Step 2: Inspect what was produced**

Run: `ls -la src/syncfield/replay/static && ls src/syncfield/replay/static/assets`

Expected: `index.html` plus an `assets/` directory containing one JS bundle and one CSS bundle.

- [ ] **Step 3: Re-run the Python smoke test against the new bundle**

Run: `cd ../../../.. && pytest tests/unit/replay -v`

Expected: All Python tests still pass — the server now serves the real bundle instead of the placeholder.

- [ ] **Step 4: Commit the built bundle**

```bash
git add src/syncfield/replay/static
git commit -m "build(replay): commit production frontend bundle"
```

---

### Task 16: Makefile targets + README section

**Files:**
- Modify: `Makefile`
- Modify: `README.md`

- [ ] **Step 1: Add Makefile targets**

Open `Makefile`. Append:

```make
.PHONY: replay-web-install replay-web-build replay-web-dev

replay-web-install:
	cd src/syncfield/replay/_web && yarn install

replay-web-build:
	cd src/syncfield/replay/_web && yarn build

replay-web-dev:
	cd src/syncfield/replay/_web && yarn dev
```

- [ ] **Step 2: Add a Replay section to `README.md`**

Open `README.md` and append (or insert near the existing viewer docs):

```markdown
## Replay a synced session

After you've recorded a session and run it through the SyncField sync
service, open the result in a local browser-based viewer:

```python
import syncfield as sf

sf.replay.launch("./data/session_2026-04-09T14-49")
```

This boots a small HTTP server on `127.0.0.1`, opens your default
browser, and shows:

- Synchronized multi-stream video playback
- A **Before / After** toggle (`B`) to compare raw vs. synced alignment
- A per-stream sync report (offset, confidence, quality)
- Minimal SVG charts for each sensor stream

Requires the `replay` extra:

```bash
pip install 'syncfield[replay]'
```

The viewer ships a pre-built React bundle, so end users do not need
Node.js or yarn — those are only needed if you want to hack on the
frontend itself (`make replay-web-dev`).
```

- [ ] **Step 3: Commit**

```bash
git add Makefile README.md
git commit -m "docs(replay): Makefile targets and README usage section"
```

---

### Task 17: Final verification — full test suite + manual smoke

**Files:** none (verification only)

- [ ] **Step 1: Run the full Python test suite**

Run: `pytest tests/unit/replay tests/unit -v 2>&1 | tail -40`

Expected: All replay tests pass, no regressions in the broader unit suite. (Some tests may be skipped if their hardware extras are not installed — that is fine.)

- [ ] **Step 2: Run the full web test suite**

Run:
```bash
cd src/syncfield/replay/_web
yarn test
```

Expected: All vitest suites pass.

- [ ] **Step 3: Manual smoke against the existing demo session**

Run:
```bash
cd ../../../..
python -c "import syncfield.replay; syncfield.replay.launch('./demo_session', open_browser=False, port=8765)" &
sleep 1
curl -s http://127.0.0.1:8765/api/session | head -c 400
kill %1
```

Expected: A JSON document starting with `{"host_id":"demo_rig",...` is printed.

- [ ] **Step 4: Final commit (if anything was tweaked) and done**

```bash
git status
# If clean, the feature is shipped.
```

---

## Spec coverage map

| Spec section | Tasks |
|---|---|
| Public API `sf.replay.launch()` | 1, 5 |
| `loader.py` + `ReplayManifest` | 2 |
| `_handler.py` path-traversal guard | 3 |
| Starlette server + routes (incl. Range) | 4 |
| `[replay]` extra + force-include | 6 |
| Vite + React + Tailwind scaffold | 7 |
| `useReplaySession` hook | 8 |
| Before/After offset math + `useBeforeAfter` | 9 |
| `SyncReportPanel` | 10 |
| `BeforeAfterToggle` | 11 |
| Multi-stream offset-aware `VideoArea` | 12 |
| Generalized `SensorChartPanel` | 13 |
| `App.tsx` shell wiring | 14 |
| Pre-built `static/` committed | 15 |
| Makefile targets + README section | 16 |
| Final smoke + regression check | 17 |
