"""Leader-side post-stop file aggregation: ``collect_from_followers()``.

Phase 5 Task 4. The leader, after ``stop()``, pulls every follower's
recorded files into a canonical tree and writes an aggregated manifest.
These tests stub out ``SessionBrowser`` and ``httpx`` so the logic is
exercised without any real network or zeroconf I/O.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

import syncfield as sf
from syncfield.multihost.types import SessionAnnouncement


# ---------------------------------------------------------------------------
# Helpers shared across cases
# ---------------------------------------------------------------------------


def _make_announcement(
    *,
    host_id: str,
    session_id: str = "amber-tiger-042",
    port: int = 7979,
    address: str = "127.0.0.1",
) -> SessionAnnouncement:
    return SessionAnnouncement(
        session_id=session_id,
        host_id=host_id,
        status="stopped",
        sdk_version="0.2.0",
        chirp_enabled=True,
        control_plane_port=port,
        resolved_address=address,
    )


class _FakeBrowser:
    """Minimal SessionBrowser stand-in used to bootstrap discovery.

    ``collect_from_followers`` constructs a fresh browser internally —
    we monkey-patch ``SessionBrowser`` in the orchestrator module to
    return one of these instead. ``current_sessions`` is fed via the
    ``announcements`` constructor arg so each test can shape its peer
    list inline.
    """

    def __init__(self, announcements):
        self._announcements = announcements
        self.start_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def current_sessions(self):
        return list(self._announcements)


def _patch_browser(monkeypatch, announcements):
    """Patch ``SessionBrowser`` symbol used inside ``orchestrator.py``."""
    fake = _FakeBrowser(announcements)
    monkeypatch.setattr(
        "syncfield.orchestrator.SessionBrowser",
        lambda session_id=None: fake,
    )
    # Skip the 1.5s mDNS-converge sleep so tests stay fast.
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return fake


def _leader(tmp_path: Path) -> sf.SessionOrchestrator:
    return sf.SessionOrchestrator(
        host_id="mac_a",
        output_dir=tmp_path,
        role=sf.LeaderRole(
            session_id="amber-tiger-042", control_plane_port=0
        ),
    )


# ---------------------------------------------------------------------------
# Validation: role + session_id preconditions
# ---------------------------------------------------------------------------


class TestPreconditions:
    def test_requires_leader_role_single_host(self, tmp_path):
        """Single-host orchestrator (no role) must reject the call."""
        session = sf.SessionOrchestrator(host_id="h", output_dir=tmp_path)
        with pytest.raises(RuntimeError, match="LeaderRole"):
            session.collect_from_followers()

    def test_requires_leader_role_follower_rejected(self, tmp_path):
        """A FollowerRole orchestrator must also reject the call."""
        session = sf.SessionOrchestrator(
            host_id="mac_b",
            output_dir=tmp_path,
            role=sf.FollowerRole(
                session_id="amber-tiger-042", control_plane_port=0
            ),
        )
        with pytest.raises(RuntimeError, match="LeaderRole"):
            session.collect_from_followers()

    def test_requires_session_id(self, tmp_path, monkeypatch):
        """If session_id resolves to None the call must raise."""
        session = _leader(tmp_path)
        # Force session_id to None — simulates pre-start state where a
        # role is set but no id has been resolved (defense-in-depth;
        # LeaderRole always has one in practice).
        monkeypatch.setattr(
            type(session),
            "session_id",
            property(lambda self: None),
        )
        with pytest.raises(RuntimeError, match="session_id"):
            session.collect_from_followers()


# ---------------------------------------------------------------------------
# Happy path: one follower, files land in <dest>/<host_id>/<path>
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_collects_from_one_follower(self, tmp_path, monkeypatch):
        session = _leader(tmp_path)
        ann = _make_announcement(host_id="mac_b", port=7979)
        _patch_browser(monkeypatch, [ann])

        # Two files reported by the follower's manifest endpoint.
        payload_a = b"hello world"
        payload_b = b"frame data" * 1000  # ~10 KiB
        files = {
            "video.mp4": payload_a,
            "audio/mic.wav": payload_b,
        }
        manifest_entries = [
            {
                "path": rel,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "mtime_ns": 1_700_000_000_000_000_000,
            }
            for rel, data in files.items()
        ]

        def fake_get(url, headers, timeout):
            assert headers["Authorization"] == "Bearer amber-tiger-042"
            assert url == "http://127.0.0.1:7979/files/manifest"
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = lambda: None
            resp.json.return_value = {"files": manifest_entries}
            return resp

        @contextmanager
        def fake_stream(method, url, headers, timeout):
            assert method == "GET"
            assert headers["Authorization"] == "Bearer amber-tiger-042"
            # url shape: http://127.0.0.1:7979/files/<rel>
            prefix = "http://127.0.0.1:7979/files/"
            assert url.startswith(prefix)
            rel = url[len(prefix):]
            data = files[rel]

            class _Resp:
                def raise_for_status(self_inner) -> None:
                    return None

                def iter_bytes(self_inner, chunk_size=65536):
                    yield data

            yield _Resp()

        monkeypatch.setattr(httpx, "get", fake_get)
        monkeypatch.setattr(httpx, "stream", fake_stream)

        dest = tmp_path / "collected"
        result = session.collect_from_followers(destination=dest)

        # Files materialised under <dest>/<host_id>/<rel>
        assert (dest / "mac_b" / "video.mp4").read_bytes() == payload_a
        assert (dest / "mac_b" / "audio" / "mic.wav").read_bytes() == payload_b

        # Aggregated manifest written + matches return value.
        on_disk = json.loads(
            (dest / "aggregated_manifest.json").read_text()
        )
        assert on_disk == result
        assert result["session_id"] == "amber-tiger-042"
        assert result["leader_host_id"] == "mac_a"
        assert len(result["hosts"]) == 1
        host = result["hosts"][0]
        assert host["host_id"] == "mac_b"
        assert host["status"] == "ok"
        assert host["error"] is None
        assert {f["path"] for f in host["files"]} == set(files.keys())

    def test_default_destination_is_data_root_session_id(
        self, tmp_path, monkeypatch
    ):
        """When no destination is passed it defaults to data_root/session_id."""
        session = _leader(tmp_path)
        # No peers — fastest happy path that still exercises destination
        # default + manifest writing.
        _patch_browser(monkeypatch, [])

        result = session.collect_from_followers()

        expected_dest = tmp_path / "amber-tiger-042"
        assert (expected_dest / "aggregated_manifest.json").exists()
        assert result["hosts"] == []

    def test_skips_self_and_peers_without_control_plane(
        self, tmp_path, monkeypatch
    ):
        """The leader itself and peers with no control_plane_port are filtered out."""
        session = _leader(tmp_path)
        anns = [
            # self — must be skipped
            _make_announcement(host_id="mac_a", port=7878),
            # follower without a control plane port — must be skipped
            SessionAnnouncement(
                session_id="amber-tiger-042",
                host_id="mac_c",
                status="stopped",
                sdk_version="0.2.0",
                chirp_enabled=True,
                control_plane_port=None,
            ),
        ]
        _patch_browser(monkeypatch, anns)

        # If filtering is broken either of these would trigger an HTTP
        # call against the unmocked httpx → AttributeError or worse.
        result = session.collect_from_followers(destination=tmp_path / "d")
        assert result["hosts"] == []


# ---------------------------------------------------------------------------
# Failure modes per host — must not abort the loop
# ---------------------------------------------------------------------------


class TestUnreachableFollower:
    def test_connect_error_marks_unreachable(self, tmp_path, monkeypatch):
        session = _leader(tmp_path)
        ann = _make_announcement(host_id="mac_b", port=7979)
        _patch_browser(monkeypatch, [ann])

        def boom(url, headers, timeout):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "get", boom)

        result = session.collect_from_followers(
            destination=tmp_path / "d"
        )
        assert len(result["hosts"]) == 1
        host = result["hosts"][0]
        assert host["host_id"] == "mac_b"
        assert host["status"] == "unreachable"
        assert "connection refused" in host["error"]
        assert host["files"] == []

    def test_one_failure_does_not_abort_other_hosts(
        self, tmp_path, monkeypatch
    ):
        """A bad follower must not prevent collection from a healthy one."""
        session = _leader(tmp_path)
        bad = _make_announcement(host_id="mac_b", port=7979)
        good = _make_announcement(host_id="mac_c", port=7980)
        _patch_browser(monkeypatch, [bad, good])

        good_payload = b"good"
        good_entry = {
            "path": "ok.bin",
            "size": len(good_payload),
            "sha256": hashlib.sha256(good_payload).hexdigest(),
            "mtime_ns": 0,
        }

        def fake_get(url, headers, timeout):
            if ":7979/" in url:
                raise httpx.ConnectError("nope")
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = lambda: None
            resp.json.return_value = {"files": [good_entry]}
            return resp

        @contextmanager
        def fake_stream(method, url, headers, timeout):
            class _R:
                def raise_for_status(self_inner) -> None:
                    return None

                def iter_bytes(self_inner, chunk_size=65536):
                    yield good_payload

            yield _R()

        monkeypatch.setattr(httpx, "get", fake_get)
        monkeypatch.setattr(httpx, "stream", fake_stream)

        result = session.collect_from_followers(
            destination=tmp_path / "d"
        )
        statuses = {h["host_id"]: h["status"] for h in result["hosts"]}
        assert statuses == {"mac_b": "unreachable", "mac_c": "ok"}
        assert (tmp_path / "d" / "mac_c" / "ok.bin").read_bytes() == good_payload


# ---------------------------------------------------------------------------
# Checksum mismatch retries exactly once before marking the host
# ---------------------------------------------------------------------------


class TestChecksumMismatch:
    def test_retries_once_then_marks_host(self, tmp_path, monkeypatch):
        session = _leader(tmp_path)
        ann = _make_announcement(host_id="mac_b", port=7979)
        _patch_browser(monkeypatch, [ann])

        # Manifest claims a digest that the body never matches → both
        # the initial download and the retry are bad → status flips to
        # checksum_mismatch.
        bogus_sha = "0" * 64
        entry = {
            "path": "video.mp4",
            "size": 5,
            "sha256": bogus_sha,
            "mtime_ns": 0,
        }

        def fake_get(url, headers, timeout):
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = lambda: None
            resp.json.return_value = {"files": [entry]}
            return resp

        download_calls: list[str] = []

        @contextmanager
        def fake_stream(method, url, headers, timeout):
            download_calls.append(url)

            class _R:
                def raise_for_status(self_inner) -> None:
                    return None

                def iter_bytes(self_inner, chunk_size=65536):
                    yield b"hello"  # sha256 != bogus_sha

            yield _R()

        monkeypatch.setattr(httpx, "get", fake_get)
        monkeypatch.setattr(httpx, "stream", fake_stream)

        result = session.collect_from_followers(
            destination=tmp_path / "d"
        )
        # Exactly two download attempts: original + one retry.
        assert len(download_calls) == 2
        host = result["hosts"][0]
        assert host["status"] == "checksum_mismatch"
        assert "video.mp4" in host["error"]
        # File was not added to the host's reported file list because
        # it failed verification.
        assert host["files"] == []

    def test_retry_succeeds_when_second_download_matches(
        self, tmp_path, monkeypatch
    ):
        """If the retry yields the right bytes, the host stays ok."""
        session = _leader(tmp_path)
        ann = _make_announcement(host_id="mac_b", port=7979)
        _patch_browser(monkeypatch, [ann])

        good_payload = b"correct"
        entry = {
            "path": "video.mp4",
            "size": len(good_payload),
            "sha256": hashlib.sha256(good_payload).hexdigest(),
            "mtime_ns": 0,
        }

        def fake_get(url, headers, timeout):
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = lambda: None
            resp.json.return_value = {"files": [entry]}
            return resp

        attempts = {"n": 0}

        @contextmanager
        def fake_stream(method, url, headers, timeout):
            attempts["n"] += 1
            payload = b"WRONG" if attempts["n"] == 1 else good_payload

            class _R:
                def raise_for_status(self_inner) -> None:
                    return None

                def iter_bytes(self_inner, chunk_size=65536):
                    yield payload

            yield _R()

        monkeypatch.setattr(httpx, "get", fake_get)
        monkeypatch.setattr(httpx, "stream", fake_stream)

        result = session.collect_from_followers(
            destination=tmp_path / "d"
        )
        assert attempts["n"] == 2
        host = result["hosts"][0]
        assert host["status"] == "ok"
        assert host["error"] is None
        assert host["files"] == [entry]
        # Final on-disk content is the good payload.
        assert (
            tmp_path / "d" / "mac_b" / "video.mp4"
        ).read_bytes() == good_payload

    def test_verify_disabled_skips_hashing(self, tmp_path, monkeypatch):
        """``verify_checksums=False`` must skip the digest check entirely."""
        session = _leader(tmp_path)
        ann = _make_announcement(host_id="mac_b", port=7979)
        _patch_browser(monkeypatch, [ann])

        bogus_sha = "0" * 64
        entry = {
            "path": "f.bin",
            "size": 5,
            "sha256": bogus_sha,
            "mtime_ns": 0,
        }

        def fake_get(url, headers, timeout):
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = lambda: None
            resp.json.return_value = {"files": [entry]}
            return resp

        download_calls = {"n": 0}

        @contextmanager
        def fake_stream(method, url, headers, timeout):
            download_calls["n"] += 1

            class _R:
                def raise_for_status(self_inner) -> None:
                    return None

                def iter_bytes(self_inner, chunk_size=65536):
                    yield b"hello"

            yield _R()

        monkeypatch.setattr(httpx, "get", fake_get)
        monkeypatch.setattr(httpx, "stream", fake_stream)

        result = session.collect_from_followers(
            destination=tmp_path / "d",
            verify_checksums=False,
        )
        # Only one download — no retry because no verification.
        assert download_calls["n"] == 1
        host = result["hosts"][0]
        assert host["status"] == "ok"
        assert host["files"] == [entry]


# ---------------------------------------------------------------------------
# Browser lifecycle: bootstrap + close in finally
# ---------------------------------------------------------------------------


class TestBrowserLifecycle:
    def test_bootstrapped_browser_is_closed(self, tmp_path, monkeypatch):
        session = _leader(tmp_path)
        fake = _patch_browser(monkeypatch, [])

        session.collect_from_followers(destination=tmp_path / "d")

        assert fake.start_calls == 1
        assert fake.close_calls == 1

    def test_browser_closed_even_on_unexpected_failure(
        self, tmp_path, monkeypatch
    ):
        """If the loop body raises, the browser must still close."""
        session = _leader(tmp_path)
        ann = _make_announcement(host_id="mac_b", port=7979)
        fake = _patch_browser(monkeypatch, [ann])

        def explode(url, headers, timeout):
            raise RuntimeError("not an httpx error — should propagate")

        monkeypatch.setattr(httpx, "get", explode)

        with pytest.raises(RuntimeError, match="propagate"):
            session.collect_from_followers(destination=tmp_path / "d")

        assert fake.close_calls == 1
