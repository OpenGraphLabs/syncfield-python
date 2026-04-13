"""Port binding fallback behavior for ControlPlaneServer."""

import socket

import pytest

from syncfield.multihost.control_plane._port_binding import (
    DEFAULT_CONTROL_PLANE_PORT,
    bind_control_plane_port,
)


class TestBindControlPlanePort:
    def test_default_preferred_port_is_7878(self) -> None:
        assert DEFAULT_CONTROL_PLANE_PORT == 7878

    def test_binds_preferred_port_when_free(self) -> None:
        # Grab a known-free port by asking the OS, then immediately close
        # it — there's a tiny race but test_binds_fallback covers the
        # occupied case.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]

        sock = bind_control_plane_port(preferred=free_port)
        try:
            assert sock.getsockname()[1] == free_port
        finally:
            sock.close()

    def test_falls_back_when_preferred_is_taken(self) -> None:
        # Occupy a port, then ask for it — bind_control_plane_port must
        # not raise and must return a socket on a different port.
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        taken_port = holder.getsockname()[1]
        try:
            sock = bind_control_plane_port(preferred=taken_port)
            try:
                chosen = sock.getsockname()[1]
                assert chosen != taken_port
                assert 1024 <= chosen <= 65535
            finally:
                sock.close()
        finally:
            holder.close()

    def test_returned_socket_is_stream_and_bound(self) -> None:
        sock = bind_control_plane_port(preferred=0)
        try:
            assert sock.type == socket.SOCK_STREAM
            assert sock.family == socket.AF_INET
            # Bound: getsockname returns a real port.
            assert sock.getsockname()[1] > 0
        finally:
            sock.close()

    def test_passing_zero_uses_os_assigned_without_fallback(self) -> None:
        sock = bind_control_plane_port(preferred=0)
        try:
            assert sock.getsockname()[1] != 0
        finally:
            sock.close()
