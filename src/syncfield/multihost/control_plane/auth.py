"""Bearer-token authentication for the HTTP control plane.

The token is the orchestrator's ``session_id``. Every host participating
in the same session knows the id, and the id is small enough to ship
in mDNS TXT, so this gives us cross-rig isolation without any key-
management ceremony. It is NOT cryptographic auth — the threat model
is "trusted local network, keep Rig A from accidentally hitting Rig B".

The dependency reads the token from the standard ``Authorization:
Bearer <token>`` header and compares it against
``request.app.state.orchestrator.session_id`` — read live on each
request so an auto-discover follower that observes its leader
mid-flight picks up the newly-known session_id without rebuilding
the app.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials


def verify_session_token(request: Request) -> None:
    """FastAPI dependency: raise 401 unless the bearer token matches the session.

    Credentials are extracted inline from the ``Authorization`` header via
    :func:`_extract_credentials`. We deliberately avoid declaring a
    ``credentials`` parameter on the signature — when the dependency is
    registered via ``dependencies=[Depends(verify_session_token)]`` on a
    route that also has a Pydantic body, FastAPI would otherwise infer a
    spurious body field from the ``HTTPAuthorizationCredentials`` type
    (which is itself a ``BaseModel``).
    """
    credentials = _extract_credentials(request)

    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        # No orchestrator wired at all — genuine server-side misconfig.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="control plane misconfigured: app.state.orchestrator is unset",
        )
    expected = orch.session_id
    if expected is None:
        # Pre-observation follower — session_id isn't known yet. Bearer
        # auth can't succeed until the follower has observed its leader.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="session_id not yet known (auto-discover follower still attaching)",
            headers={"Retry-After": "2"},
        )

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if credentials.credentials != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _extract_credentials(request: Request) -> HTTPAuthorizationCredentials | None:
    header = request.headers.get("Authorization", "")
    if not header:
        return None
    parts = header.split(" ", 1)
    if len(parts) != 2:
        return None
    return HTTPAuthorizationCredentials(scheme=parts[0], credentials=parts[1])
