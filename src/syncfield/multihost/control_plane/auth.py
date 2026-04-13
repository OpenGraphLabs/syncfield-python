"""Bearer-token authentication for the HTTP control plane.

The token is the orchestrator's ``session_id``. Every host participating
in the same session knows the id, and the id is small enough to ship
in mDNS TXT, so this gives us cross-rig isolation without any key-
management ceremony. It is NOT cryptographic auth — the threat model
is "trusted local network, keep Rig A from accidentally hitting Rig B".

The dependency reads the token from the standard ``Authorization:
Bearer <token>`` header and compares it against
``request.app.state.session_id``, which the orchestrator sets when it
constructs the FastAPI app.
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

    expected = getattr(request.app.state, "session_id", None)
    if expected is None:
        # The orchestrator forgot to attach state.session_id. This is a
        # server-side misconfiguration; return 500, not 401, so it
        # doesn't masquerade as a client auth failure.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="control plane misconfigured: app.state.session_id is unset",
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
