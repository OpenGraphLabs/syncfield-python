"""Unit tests for the macOS dns-sd subprocess fallback."""

import platform
from unittest.mock import MagicMock, patch

import pytest

from syncfield.multihost import _dns_sd_fallback as fb


SAMPLE_DNS_SD_OUTPUT = (
    "Lookup lab_session--mac_a._syncfield._tcp.local.\n"
    "DATE: ---Mon 13 Apr 2026---\n"
    "16:30:00.000  ...STARTING...\n"
    "16:30:00.012  lab_session--mac_a._syncfield._tcp.local. "
    "can be reached at testhost.local.:7878 (interface 14)\n"
    " host_id=mac_a session_id=lab_session sdk_version=0.3.6 chirp_enabled=1\n"
)


class TestStripServiceSuffix:
    def test_strips_full_suffix(self):
        result = fb._strip_service_suffix(
            "foo._syncfield._tcp.local.",
            "_syncfield._tcp.local.",
        )
        assert result == "foo"

    def test_handles_double_dash_in_instance(self):
        result = fb._strip_service_suffix(
            "lab_session--mac_a._syncfield._tcp.local.",
            "_syncfield._tcp.local.",
        )
        assert result == "lab_session--mac_a"

    def test_returns_none_when_suffix_doesnt_match(self):
        result = fb._strip_service_suffix(
            "foo._other._tcp.local.",
            "_syncfield._tcp.local.",
        )
        assert result is None


class TestResolveViaDnsSd:
    def test_returns_none_on_non_macos(self, monkeypatch):
        monkeypatch.setattr(fb.platform, "system", lambda: "Linux")
        assert fb.resolve_via_dns_sd(
            "x._syncfield._tcp.local.", "_syncfield._tcp.local."
        ) is None

    def test_parses_dns_sd_output(self, monkeypatch):
        monkeypatch.setattr(fb.platform, "system", lambda: "Darwin")

        # Build a fake subprocess whose stdout yields our sample lines.
        # Simulate select() always reporting ready, then EOF.
        fake_proc = MagicMock()
        fake_proc.stdout.readline.side_effect = SAMPLE_DNS_SD_OUTPUT.splitlines(keepends=True) + [""]
        fake_proc.terminate = MagicMock()
        fake_proc.wait = MagicMock()

        monkeypatch.setattr(fb.subprocess, "Popen", MagicMock(return_value=fake_proc))
        monkeypatch.setattr(fb.select, "select", lambda r, w, x, t: (r, [], []))
        monkeypatch.setattr(fb.socket, "gethostbyname", lambda h: "192.168.1.5")
        monkeypatch.setattr(fb.socket, "inet_aton", lambda a: b"\xc0\xa8\x01\x05")

        info = fb.resolve_via_dns_sd(
            "lab_session--mac_a._syncfield._tcp.local.",
            "_syncfield._tcp.local.",
        )
        assert info is not None
        assert info.port == 7878
        assert info.properties[b"host_id"] == b"mac_a"
        assert info.properties[b"session_id"] == b"lab_session"
        assert info.properties[b"sdk_version"] == b"0.3.6"
        assert info.properties[b"chirp_enabled"] == b"1"
        assert info.parsed_addresses() == ["192.168.1.5"]

    def test_returns_none_when_subprocess_spawn_fails(self, monkeypatch):
        monkeypatch.setattr(fb.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            fb.subprocess, "Popen",
            MagicMock(side_effect=FileNotFoundError("dns-sd not found")),
        )
        assert fb.resolve_via_dns_sd(
            "x._syncfield._tcp.local.", "_syncfield._tcp.local."
        ) is None

    def test_falls_back_to_hostname_when_ip_resolution_fails(self, monkeypatch):
        """When socket.gethostbyname + dns-sd -G all fail, we still
        return success with the .local hostname stored on the value
        object — httpx + macOS getaddrinfo can resolve it at
        request time via mDNSResponder."""
        import socket as real_socket
        monkeypatch.setattr(fb.platform, "system", lambda: "Darwin")

        def make_fake_proc():
            p = MagicMock()
            p.stdout.readline.side_effect = (
                SAMPLE_DNS_SD_OUTPUT.splitlines(keepends=True) + [""]
            )
            return p

        monkeypatch.setattr(
            fb.subprocess, "Popen", MagicMock(side_effect=lambda *a, **kw: make_fake_proc())
        )
        monkeypatch.setattr(fb.select, "select", lambda r, w, x, t: (r, [], []))
        monkeypatch.setattr(
            fb.socket, "gethostbyname",
            MagicMock(side_effect=real_socket.gaierror("not found")),
        )
        monkeypatch.setattr(
            fb.socket, "inet_aton",
            MagicMock(side_effect=OSError("no addr")),
        )

        info = fb.resolve_via_dns_sd(
            "lab_session--mac_a._syncfield._tcp.local.",
            "_syncfield._tcp.local.",
        )
        assert info is not None
        assert info.port == 7878
        # Empty packed addresses, but hostname-only fallback populates
        # parsed_addresses() with the .local hostname.
        assert info._addresses == []
        assert info.hostname == "testhost.local"
        assert info.parsed_addresses() == ["testhost.local"]


class TestResolvedInfo:
    def test_parsed_addresses_decodes_bytes(self):
        info = fb._ResolvedInfo(
            port=7878,
            properties={b"host_id": b"mac_a"},
            _addresses=[b"\xc0\xa8\x01\x05"],
        )
        assert info.parsed_addresses() == ["192.168.1.5"]
        assert info.port == 7878
        assert info.properties[b"host_id"] == b"mac_a"
