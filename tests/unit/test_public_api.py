"""Tests for the public import surface of the syncfield package."""

from __future__ import annotations


def test_top_level_exports():
    import syncfield as sf
    # Core orchestrator API
    assert hasattr(sf, "SessionOrchestrator")
    assert hasattr(sf, "SyncToneConfig")
    assert hasattr(sf, "ChirpSpec")
    assert hasattr(sf, "ChirpEmission")
    assert hasattr(sf, "ChirpSource")
    # Protocol + base class for adapter authors
    assert hasattr(sf, "Stream")
    assert hasattr(sf, "StreamBase")
    # Key types
    assert hasattr(sf, "StreamCapabilities")
    assert hasattr(sf, "SessionState")
    assert hasattr(sf, "SyncPoint")
    # Multi-host roles (opt-in)
    assert hasattr(sf, "LeaderRole")
    assert hasattr(sf, "FollowerRole")
    assert hasattr(sf, "RoleKind")
    # Clock
    assert hasattr(sf, "SessionClock")
    # Version
    assert hasattr(sf, "__version__")


def test_multihost_subpackage():
    """The multi-host rendezvous subpackage is separately importable."""
    from syncfield.multihost import (
        SERVICE_TYPE,
        SessionAdvertStatus,
        SessionAdvertiser,
        SessionAnnouncement,
        SessionBrowser,
        generate_session_id,
        is_valid_session_id,
    )
    assert SERVICE_TYPE == "_syncfield._tcp.local."
    assert callable(generate_session_id)
    assert is_valid_session_id("amber-tiger-042")
    # Classes are exposed for type hints / custom orchestration
    assert SessionAdvertiser is not None
    assert SessionBrowser is not None
    assert SessionAnnouncement is not None
    # Literal type alias — just check it is importable, not its shape
    assert SessionAdvertStatus is not None


def test_testing_subpackage():
    from syncfield.testing import FakeStream
    assert FakeStream("x").id == "x"


def test_adapters_subpackage_jsonl_always_importable():
    from syncfield.adapters import JSONLFileStream
    assert JSONLFileStream is not None


def test_no_old_sync_session_export():
    import syncfield as sf
    assert not hasattr(sf, "SyncSession")
