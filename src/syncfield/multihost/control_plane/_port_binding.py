"""TCP socket pre-binding for the control plane.

The control plane's uvicorn server accepts a file descriptor via its
``fd=`` kwarg. We pre-bind the socket in Python so that:

1. We can inspect the chosen port (handy when the preferred port was
   taken and the OS assigned something else).
2. We eliminate the tiny race between "probe for a free port" and
   "uvicorn binds" — the socket is *our* socket from the moment it's
   bound.

The preferred default is 7878. Callers can override by passing
``preferred=``. Passing ``preferred=0`` short-circuits the fallback
path and goes straight to OS-assigned. Any other preferred value that
fails to bind (``OSError`` with EADDRINUSE) falls back to OS-assigned
transparently.
"""

from __future__ import annotations

import errno
import logging
import socket

logger = logging.getLogger(__name__)

#: Default preferred port. Picked to avoid well-known IANA ports and
#: to be memorable. Followers advertise the *actually bound* port via
#: mDNS ``ServiceInfo.port`` so this number never needs to be dialed
#: in directly by peers.
DEFAULT_CONTROL_PLANE_PORT = 7878


def bind_control_plane_port(
    preferred: int = DEFAULT_CONTROL_PLANE_PORT,
    *,
    host: str = "0.0.0.0",
) -> socket.socket:
    """Bind a TCP socket on ``preferred`` (falling back to OS-assigned).

    Args:
        preferred: Port number to attempt first. Pass ``0`` to skip the
            preferred attempt and go straight to OS-assigned.
        host: Interface address. Defaults to ``0.0.0.0`` so the control
            plane is reachable from other hosts on the LAN.

    Returns:
        A TCP stream socket already bound to the chosen port. Caller
        must ``listen()`` / hand it to uvicorn and close it when done.

    Raises:
        OSError: Only if the OS-assigned fallback itself fails —
            which would indicate something genuinely broken with the
            networking stack.
    """
    if preferred == 0:
        return _bind_os_assigned(host)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, preferred))
        return sock
    except OSError as exc:
        if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
            sock.close()
            raise
        sock.close()
        logger.info(
            "Preferred control-plane port %d unavailable (%s); "
            "falling back to OS-assigned.",
            preferred,
            exc,
        )
        return _bind_os_assigned(host)


def _bind_os_assigned(host: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    return sock
