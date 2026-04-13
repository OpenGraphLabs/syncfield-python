"""FastAPI app factory and route handlers for the control plane.

Every endpoint uses the :func:`~syncfield.multihost.control_plane.auth.verify_session_token`
dependency to enforce bearer auth. The app's dependency on the actual
orchestrator is deliberately **shape-based** rather than type-based:
``build_control_plane_app`` takes any object that exposes the
attributes and methods exercised below. The real
:class:`~syncfield.orchestrator.SessionOrchestrator` conforms; test
doubles (see ``tests/unit/multihost/control_plane/test_routes.py``)
can provide a minimal fake without inheriting.

Phase 4 makes the ``/session/config`` endpoints real: POST parses the
body into a :class:`~syncfield.multihost.session_config.SessionConfig`
dataclass, validates it against this host's local capabilities, and
stores the applied config on ``app.state.applied_config`` (or returns
HTTP 400 with the validation error message verbatim). GET returns
404 until something has been applied.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from fastapi import Depends, FastAPI, HTTPException, Request, status

from syncfield.multihost.control_plane.auth import verify_session_token
from syncfield.multihost.control_plane.schemas import (
    ChirpSpecModel,
    HealthResponse,
    SessionConfigRequest,
    SessionConfigResponse,
    SessionStateResponse,
    StreamHealth,
    StreamsResponse,
)
from syncfield.multihost.session_config import (
    SessionConfig,
    validate_config_against_local_capabilities,
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
    has_audio_stream: bool
    supported_audio_range_hz: "tuple[float, float]"

    def snapshot_stream_metrics(self) -> list: ...
    def trigger_start(self) -> str: ...
    def trigger_stop(self) -> str: ...
    def trigger_control_plane_shutdown(self) -> None: ...
    def apply_distributed_config(self, config) -> None: ...


def _chirp_from_model(m: ChirpSpecModel) -> "ChirpSpec":
    from syncfield.types import ChirpSpec

    return ChirpSpec(
        from_hz=m.from_hz,
        to_hz=m.to_hz,
        duration_ms=m.duration_ms,
        amplitude=m.amplitude,
        envelope_ms=m.envelope_ms,
    )


def _response_from_config(cfg: "SessionConfig") -> SessionConfigResponse:
    return SessionConfigResponse(
        session_name=cfg.session_name,
        start_chirp=ChirpSpecModel(**cfg.start_chirp.to_dict()),
        stop_chirp=ChirpSpecModel(**cfg.stop_chirp.to_dict()),
        recording_mode=cfg.recording_mode,
    )


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
    app.state.applied_config = None  # SessionConfig | None; populated on first POST or GET-from-leader

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
        """Validate the leader's proposed config and apply locally.

        Validation runs against the local host's capabilities:
        ``has_audio_stream`` (must be True) and
        ``supported_audio_range_hz`` (chirp frequencies must fit). A
        ``ValueError`` from
        :func:`~syncfield.multihost.session_config.validate_config_against_local_capabilities`
        becomes HTTP 400 with the message verbatim — the leader wraps
        per-host 400s into a :class:`~syncfield.multihost.errors.ClusterConfigMismatch`
        and aborts the start.
        """
        orch = request.app.state.orchestrator
        cfg = SessionConfig(
            session_name=body.session_name,
            start_chirp=_chirp_from_model(body.start_chirp),
            stop_chirp=_chirp_from_model(body.stop_chirp),
            recording_mode=body.recording_mode,
        )
        try:
            validate_config_against_local_capabilities(
                cfg,
                has_audio_stream=orch.has_audio_stream,
                supported_audio_range_hz=orch.supported_audio_range_hz,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )
        request.app.state.applied_config = cfg
        # Also propagate to the orchestrator so stop() embeds this in
        # the manifest. C2 fix: without this, follower manifests
        # silently omit session_config on the POST-arrived path.
        orch.apply_distributed_config(cfg)
        return _response_from_config(cfg)

    @app.get(
        "/session/config",
        response_model=SessionConfigResponse,
        dependencies=[Depends(verify_session_token)],
    )
    def session_config_get(request: Request) -> SessionConfigResponse:
        cfg = request.app.state.applied_config
        if cfg is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no session config applied yet",
            )
        return _response_from_config(cfg)

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
