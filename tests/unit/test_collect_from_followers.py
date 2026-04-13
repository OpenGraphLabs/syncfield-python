"""Leader-side post-stop file aggregation: ``collect_from_followers()``.

Phase 5 Task 4 (refactored to flat layout). The leader, after ``stop()``,
pulls every host's files — its own plus every follower's — into a single
flat episode directory rooted at the leader's episode name, and writes
an aggregated manifest alongside it.

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


def _leader_episode_name(leader: sf.SessionOrchestrator) -> str:
    """The canonical episode dir name the leader records into."""
    return leader._output_dir.name


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
# Flatten helper
# ---------------------------------------------------------------------------


class TestFlattenFollowerPath:
    def test_strips_episode_prefix(self):
        assert (
            sf.SessionOrchestrator._flatten_follower_path(
                "mac_b", "ep_20260413_xxx/host_audio.wav"
            )
            == "mac_b.host_audio.wav"
        )

    def test_flattens_subdirs(self):
        assert (
            sf.SessionOrchestrator._flatten_follower_path(
                "mac_b", "ep_xxx/subdir/file.bin"
            )
            == "mac_b.subdir.file.bin"
        )

    def test_no_episode_prefix(self):
        assert (
            sf.SessionOrchestrator._flatten_follower_path(
                "mac_b", "loose_file.txt"
            )
            == "mac_b.loose_file.txt"
        )


# ---------------------------------------------------------------------------
# Happy path: one follower, files land flat under <dest>/<leader_ep>/
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_collects_from_one_follower(self, tmp_path, monkeypatch):
        session = _leader(tmp_path)
        leader_ep = _leader_episode_name(session)
        ann = _make_announcement(host_id="mac_b", port=7979)
        _patch_browser(monkeypatch, [ann])

        # Two files reported by the follower's manifest endpoint. The
        # follower's manifest paths are relative to its host_output_dir
        # and begin with an ``ep_*/`` segment (a different episode name
        # than the leader's — that's the whole point of the flattening).
        follower_ep = "ep_20260413_014200_beef01"
        payload_a = b"hello world"
        payload_b = b"frame data" * 1000  # ~10 KiB
        files = {
            f"{follower_ep}/video.mp4": payload_a,
            f"{follower_ep}/audio/mic.wav": payload_b,
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
            # url shape: http://127.0.0.1:7979/files/<raw_rel>
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

        # Files materialised at <dest>/<leader_ep>/<host>.<flat_path>.
        assert (
            dest / leader_ep / "mac_b.video.mp4"
        ).read_bytes() == payload_a
        assert (
            dest / leader_ep / "mac_b.audio.mic.wav"
        ).read_bytes() == payload_b

        # Aggregated manifest written at <dest>/aggregated_manifest.json
        # and matches the return value.
        on_disk = json.loads(
            (dest / "aggregated_manifest.json").read_text()
        )
        assert on_disk == result
        assert result["session_id"] == "amber-tiger-042"
        assert result["leader_host_id"] == "mac_a"
        assert result["leader_episode"] == leader_ep

        # Leader's self-entry is the first host in the report. Its
        # _output_dir doesn't exist (no recording happened), so it's
        # reported as "missing" — that's the expected contract for the
        # no-recording test setup.
        assert len(result["hosts"]) == 2
        leader_entry = result["hosts"][0]
        assert leader_entry["host_id"] == "mac_a"
        assert leader_entry["status"] == "missing"

        # Follower entry.
        host = result["hosts"][1]
        assert host["host_id"] == "mac_b"
        assert host["status"] == "ok"
        assert host["error"] is None
        assert {f["path"] for f in host["files"]} == {
            "mac_b.video.mp4",
            "mac_b.audio.mic.wav",
        }

    def test_copies_leader_files_into_flat_episode_dir(
        self, tmp_path, monkeypatch
    ):
        """Leader's own recordings are copied + flattened into the episode dir."""
        session = _leader(tmp_path)
        leader_ep = _leader_episode_name(session)
        # Simulate that recording happened: seed the leader's episode
        # dir with a couple of files. _output_dir points to
        # <tmp_path>/amber-tiger-042/mac_a/<leader_ep>/.
        session._output_dir.mkdir(parents=True, exist_ok=True)
        (session._output_dir / "host_audio.wav").write_bytes(b"leader-audio")
        (session._output_dir / "manifest.json").write_text("{}")
        (session._output_dir / "subdir").mkdir()
        (session._output_dir / "subdir" / "file.bin").write_bytes(b"xx")

        _patch_browser(monkeypatch, [])  # no followers

        dest = tmp_path / "collected"
        result = session.collect_from_followers(destination=dest)

        # Flat files with mac_a. prefix under the leader's episode dir.
        assert (
            dest / leader_ep / "mac_a.host_audio.wav"
        ).read_bytes() == b"leader-audio"
        assert (
            dest / leader_ep / "mac_a.manifest.json"
        ).read_text() == "{}"
        assert (
            dest / leader_ep / "mac_a.subdir.file.bin"
        ).read_bytes() == b"xx"

        # Leader's Phase-1 host dir was removed (default keep_leader_originals=False).
        leader_host_dir = tmp_path / "amber-tiger-042" / "mac_a"
        assert not leader_host_dir.exists()

        # Leader entry in the aggregate is "ok" with the flat paths.
        assert len(result["hosts"]) == 1
        leader_entry = result["hosts"][0]
        assert leader_entry["host_id"] == "mac_a"
        assert leader_entry["status"] == "ok"
        assert {f["path"] for f in leader_entry["files"]} == {
            "mac_a.host_audio.wav",
            "mac_a.manifest.json",
            "mac_a.subdir.file.bin",
        }

    def test_keep_leader_originals_preserves_host_subtree(
        self, tmp_path, monkeypatch
    ):
        """``keep_leader_originals=True`` leaves the Phase-1 host dir intact."""
        session = _leader(tmp_path)
        session._output_dir.mkdir(parents=True, exist_ok=True)
        (session._output_dir / "host_audio.wav").write_bytes(b"x")

        _patch_browser(monkeypatch, [])

        dest = tmp_path / "collected"
        session.collect_from_followers(
            destination=dest, keep_leader_originals=True
        )

        leader_host_dir = tmp_path / "amber-tiger-042" / "mac_a"
        assert leader_host_dir.exists()
        assert (session._output_dir / "host_audio.wav").exists()

    def test_default_destination_is_data_root_session_id(
        self, tmp_path, monkeypatch
    ):
        """When no destination is passed it defaults to data_root/session_id."""
        session = _leader(tmp_path)
        leader_ep = _leader_episode_name(session)
        # No peers — fastest happy path that still exercises destination
        # default + manifest writing.
        _patch_browser(monkeypatch, [])

        result = session.collect_from_followers()

        expected_root = tmp_path / "amber-tiger-042"
        assert (expected_root / "aggregated_manifest.json").exists()
        # Episode dir is created even when leader has no files.
        assert (expected_root / leader_ep).exists()
        # Leader self-entry only (no followers).
        assert len(result["hosts"]) == 1
        assert result["hosts"][0]["host_id"] == "mac_a"

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
        # Only the leader's own self-entry is present.
        assert len(result["hosts"]) == 1
        assert result["hosts"][0]["host_id"] == "mac_a"


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
        # [0] leader self-entry, [1] follower.
        assert len(result["hosts"]) == 2
        host = result["hosts"][1]
        assert host["host_id"] == "mac_b"
        assert host["status"] == "unreachable"
        assert "connection refused" in host["error"]
        assert host["files"] == []

    def test_one_failure_does_not_abort_other_hosts(
        self, tmp_path, monkeypatch
    ):
        """A bad follower must not prevent collection from a healthy one."""
        session = _leader(tmp_path)
        leader_ep = _leader_episode_name(session)
        bad = _make_announcement(host_id="mac_b", port=7979)
        good = _make_announcement(host_id="mac_c", port=7980)
        _patch_browser(monkeypatch, [bad, good])

        good_payload = b"good"
        good_entry = {
            "path": "ep_xxx/ok.bin",
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
        # Leader is "missing" (no recording), bad follower unreachable,
        # good follower ok.
        assert statuses == {
            "mac_a": "missing",
            "mac_b": "unreachable",
            "mac_c": "ok",
        }
        assert (
            tmp_path / "d" / leader_ep / "mac_c.ok.bin"
        ).read_bytes() == good_payload


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
            "path": "ep_xxx/video.mp4",
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
        # [0] leader, [1] follower.
        host = result["hosts"][1]
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
        leader_ep = _leader_episode_name(session)
        ann = _make_announcement(host_id="mac_b", port=7979)
        _patch_browser(monkeypatch, [ann])

        good_payload = b"correct"
        entry = {
            "path": "ep_xxx/video.mp4",
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
        # [0] leader, [1] follower.
        host = result["hosts"][1]
        assert host["status"] == "ok"
        assert host["error"] is None
        assert len(host["files"]) == 1
        assert host["files"][0]["path"] == "mac_b.video.mp4"
        assert host["files"][0]["sha256"] == entry["sha256"]
        # Final on-disk content is the good payload at the flat path.
        assert (
            tmp_path / "d" / leader_ep / "mac_b.video.mp4"
        ).read_bytes() == good_payload

    def test_verify_disabled_skips_hashing(self, tmp_path, monkeypatch):
        """``verify_checksums=False`` must skip the digest check entirely."""
        session = _leader(tmp_path)
        ann = _make_announcement(host_id="mac_b", port=7979)
        _patch_browser(monkeypatch, [ann])

        bogus_sha = "0" * 64
        entry = {
            "path": "ep_xxx/f.bin",
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
        # [0] leader, [1] follower.
        host = result["hosts"][1]
        assert host["status"] == "ok"
        assert len(host["files"]) == 1
        assert host["files"][0]["path"] == "mac_b.f.bin"


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
