"""FastAPI app factory and route handlers for the control plane.

Every endpoint uses the :func:`~syncfield.multihost.control_plane.auth.verify_session_token`
dependency to enforce bearer auth. The app's dependency on the actual
orchestrator is deliberately **shape-based** rather than type-based:
``build_control_plane_app`` takes any object that exposes the
attributes and methods exercised below. The real
:class:`~syncfield.orchestrator.SessionOrchestrator` conforms; test
doubles (see ``tests/unit/multihost/control_plane/test_routes.py``)
can provide a minimal fake without inheriting.

The ``/session/config`` endpoints keep a small in-memory dict on
``app.state.session_config`` in Phase 3. Phase 4 will replace this
with a real flow that pushes validated config to followers over the
same endpoint.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Protocol, runtime_checkable

from fastapi import Depends, FastAPI, Request

from syncfield.multihost.control_plane.auth import verify_session_token
from syncfield.multihost.control_plane.schemas import (
    HealthResponse,
    SessionConfigRequest,
    SessionConfigResponse,
    SessionStateResponse,
    StreamHealth,
    StreamsResponse,
)


@runtime_checkable
class _OrchestratorLike(Protocol):
    """Shape the control plane routes require from the orchestrator.

    Formalized for type-checker clarity only — FastAPI never looks at
    this protocol at runtime; the test harness passes a dataclass
    that satisfies it structurally.
    """

    host_id: str
    session_id: str
    role_kind: "str | None"
    state_name: str
    sdk_version: str

    def snapshot_stream_metrics(self) -> list: ...
    def trigger_start(self) -> str: ...
    def trigger_stop(self) -> str: ...
    def trigger_control_plane_shutdown(self) -> None: ...


def build_control_plane_app(
    orchestrator: _OrchestratorLike,
    *,
    started_at_monotonic_s: "float | None" = None,
) -> FastAPI:
    """Assemble the FastAPI app that serves the control plane.

    Args:
        orchestrator: The live orchestrator instance (or a test double
            that conforms to the same shape).
        started_at_monotonic_s: Monotonic reference used to compute
            ``/health`` uptime. Defaults to ``time.monotonic()`` at the
            moment the app is built.
    """
    app = FastAPI(
        title="SyncField Control Plane",
        version="0.1.0",
        docs_url=None,  # no interactive docs on a LAN-only service
        redoc_url=None,
    )

    app.state.orchestrator = orchestrator
    app.state.session_id = orchestrator.session_id
    app.state.started_at_monotonic_s = (
        started_at_monotonic_s if started_at_monotonic_s is not None else time.monotonic()
    )
    app.state.session_config = {}  # TODO(phase-4): replace in-memory stub with validated + distributed config flow

    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    @app.get(
        "/health",
        response_model=HealthResponse,
        dependencies=[Depends(verify_session_token)],
    )
    def health(request: Request) -> HealthResponse:
        orch = request.app.state.orchestrator
        started = request.app.state.started_at_monotonic_s
        return HealthResponse(
            host_id=orch.host_id,
            role=orch.role_kind,
            state=orch.state_name,
            sdk_version=orch.sdk_version,
            uptime_s=max(0.0, time.monotonic() - started),
        )

    @app.get(
        "/streams",
        response_model=StreamsResponse,
        dependencies=[Depends(verify_session_token)],
    )
    def streams(request: Request) -> StreamsResponse:
        orch = request.app.state.orchestrator
        metrics = orch.snapshot_stream_metrics()
        return StreamsResponse(
            streams=[
                StreamHealth(
                    id=m.id,
                    kind=m.kind,
                    fps=m.fps,
                    frames=m.frames,
                    dropped=m.dropped,
                    last_frame_ns=m.last_frame_ns,
                    bytes_written=m.bytes_written,
                )
                for m in metrics
            ]
        )

    @app.post(
        "/session/start",
        response_model=SessionStateResponse,
        dependencies=[Depends(verify_session_token)],
    )
    def session_start(request: Request) -> SessionStateResponse:
        orch = request.app.state.orchestrator
        new_state = orch.trigger_start()
        return SessionStateResponse(
            state=new_state,
            detail="start triggered via control plane",
        )

    @app.post(
        "/session/stop",
        response_model=SessionStateResponse,
        dependencies=[Depends(verify_session_token)],
    )
    def session_stop(request: Request) -> SessionStateResponse:
        orch = request.app.state.orchestrator
        new_state = orch.trigger_stop()
        return SessionStateResponse(
            state=new_state,
            detail="stop triggered via control plane",
        )

    @app.post(
        "/session/config",
        response_model=SessionConfigResponse,
        dependencies=[Depends(verify_session_token)],
    )
    def session_config_post(
        request: Request, body: SessionConfigRequest
    ) -> SessionConfigResponse:
        """Replace the entire session config.

        **POST is a full replace, not a patch.** The stored config is
        overwritten with the request body verbatim (including ``None``
        values for omitted fields). This semantic stays in Phase 3;
        Phase 4 may introduce a PATCH variant or a richer merge
        policy once the config distribution flow is designed.
        """
        stored: Dict[str, Any] = body.model_dump(exclude_unset=False)
        request.app.state.session_config = stored
        return SessionConfigResponse(**stored)

    @app.get(
        "/session/config",
        response_model=SessionConfigResponse,
        dependencies=[Depends(verify_session_token)],
    )
    def session_config_get(request: Request) -> SessionConfigResponse:
        stored = request.app.state.session_config or {}
        return SessionConfigResponse(**stored)

    @app.delete(
        "/session",
        response_model=SessionStateResponse,
        dependencies=[Depends(verify_session_token)],
    )
    def session_delete(request: Request) -> SessionStateResponse:
        orch = request.app.state.orchestrator
        orch.trigger_control_plane_shutdown()
        return SessionStateResponse(
            state="shutting_down",
            detail="control plane teardown requested",
        )
