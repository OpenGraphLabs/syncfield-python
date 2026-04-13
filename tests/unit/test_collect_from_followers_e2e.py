"""End-to-end: ``collect_from_followers`` over real uvicorn + httpx.

Phase 5 Task 5. Mirrors :mod:`test_collect_from_followers`, but instead
of mocking ``httpx`` we boot a real follower control plane on
``127.0.0.1:0`` and let the leader drive an actual HTTP transfer
against the FastAPI ``/files/manifest`` and ``/files/{path}`` routes.

Only ``SessionBrowser`` is stubbed — discovery is out of scope for this
test (it would require zeroconf on localhost and racy waits). The
remaining stack — token auth, JSON manifest, file streaming, sha256
verification, on-disk materialization, aggregated_manifest.json — is
exercised end-to-end.
"""

from __future__ import annotations

import json

import syncfield as sf
from syncfield.multihost.types import SessionAnnouncement
from tests.unit.conftest import FakeStream


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_follower(tmp_path) -> "sf.SessionOrchestrator":
    """Build a follower orchestrator with audio + a video stream.

    Audio is required by ``_validate_multihost_audio_requirement``,
    which fires inside ``_start_control_plane_only_for_tests``.
    """
    follower = sf.SessionOrchestrator(
        host_id="mac_b",
        output_dir=tmp_path / "follower_data",
        role=sf.FollowerRole(
            session_id="amber-tiger-042",
            control_plane_port=0,
        ),
    )
    follower.add(FakeStream("cam"))
    mic = FakeStream("mic")
    mic.kind = "audio"
    follower.add(mic)
    return follower


def _seed_follower_files(tmp_path) -> "tuple[object, object]":
    """Pre-populate the follower's host output directory with two files.

    Returns the (host_dir, episode_dir) tuple. We point ``_output_dir``
    at ``host_dir / 'ep_*'`` so ``host_output_dir()`` (which walks one
    level up from ``_output_dir``) returns the host_dir, and place the
    fixture files directly under host_dir so they materialise at
    ``destination/<host_id>/<rel>`` without a synthetic ep_* prefix.
    """
    host_dir = tmp_path / "follower_data" / "amber-tiger-042" / "mac_b"
    episode_dir = host_dir / "ep_20260412_000000_abc123"
    episode_dir.mkdir(parents=True)

    (host_dir / "test1.txt").write_text("hello")
    (host_dir / "nested").mkdir()
    (host_dir / "nested" / "test2.bin").write_bytes(b"\x00\x01\x02\x03\x04")

    return host_dir, episode_dir


def _build_leader(tmp_path) -> "sf.SessionOrchestrator":
    leader = sf.SessionOrchestrator(
        host_id="mac_a",
        output_dir=tmp_path / "leader_data",
        role=sf.LeaderRole(
            session_id="amber-tiger-042",
            control_plane_port=0,
        ),
    )
    mic = FakeStream("leader_mic")
    mic.kind = "audio"
    leader.add(mic)
    return leader


def _patch_browser_with_announcement(
    monkeypatch, announcement: SessionAnnouncement
) -> None:
    """Make the leader's internal SessionBrowser yield exactly one peer.

    ``collect_from_followers`` constructs ``SessionBrowser`` directly
    from the orchestrator module's namespace, so we patch the symbol
    bound there (not in ``syncfield.multihost.browser``).
    """

    class _FakeBrowser:
        def __init__(self, session_id=None):
            self.session_id = session_id

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def current_sessions(self):
            return [announcement]

    monkeypatch.setattr(
        "syncfield.orchestrator.SessionBrowser", _FakeBrowser
    )
    # Skip the 1.5s mDNS-converge sleep.
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCollectFromFollowersE2E:
    def test_happy_path_pulls_real_files_over_http(
        self, tmp_path, monkeypatch
    ) -> None:
        """A live follower control plane serves files the leader pulls."""
        follower = _make_follower(tmp_path)
        _, episode_dir = _seed_follower_files(tmp_path)

        # Force the orchestrator to treat the seeded tree as its
        # episode dir so host_output_dir() returns the parent host_dir
        # populated by _seed_follower_files().
        follower._output_dir = episode_dir
        follower._episode_dir_created = True
        follower._start_control_plane_only_for_tests()
        try:
            port = follower._control_plane.actual_port
            ann = SessionAnnouncement(
                session_id="amber-tiger-042",
                host_id="mac_b",
                status="stopped",
                sdk_version="0.2.0",
                chirp_enabled=True,
                control_plane_port=port,
                resolved_address="127.0.0.1",
            )
            _patch_browser_with_announcement(monkeypatch, ann)

            leader = _build_leader(tmp_path)

            dest = tmp_path / "aggregated"
            result = leader.collect_from_followers(destination=dest)
        finally:
            follower._stop_control_plane_only_for_tests()

        # Files arrived with correct bytes.
        assert (dest / "mac_b" / "test1.txt").read_text() == "hello"
        assert (
            dest / "mac_b" / "nested" / "test2.bin"
        ).read_bytes() == b"\x00\x01\x02\x03\x04"

        # Aggregated manifest written, matches return value, reports ok.
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
        # Both files are reported in the host's manifest.
        rels = {entry["path"] for entry in host["files"]}
        assert rels == {"test1.txt", "nested/test2.bin"}

    def test_unreachable_follower_marks_host_without_raising(
        self, tmp_path, monkeypatch
    ) -> None:
        """A peer with no listener becomes ``status=unreachable`` cleanly."""
        # Point the leader at a port nothing is bound to. We don't
        # boot a follower at all — this guarantees the connect fails.
        ann = SessionAnnouncement(
            session_id="amber-tiger-042",
            host_id="mac_b",
            status="stopped",
            sdk_version="0.2.0",
            chirp_enabled=True,
            control_plane_port=59999,
            resolved_address="127.0.0.1",
        )
        _patch_browser_with_announcement(monkeypatch, ann)

        leader = _build_leader(tmp_path)
        dest = tmp_path / "aggregated"
        # Tight timeout so the test stays fast even if the kernel
        # decides to be polite about RST handling.
        result = leader.collect_from_followers(
            destination=dest, timeout=1.0
        )

        assert (dest / "aggregated_manifest.json").exists()
        assert len(result["hosts"]) == 1
        host = result["hosts"][0]
        assert host["host_id"] == "mac_b"
        assert host["status"] == "unreachable"
        assert host["error"] is not None
        assert host["files"] == []
