"""Tests for the public import surface of the syncfield package."""

from __future__ import annotations


def test_top_level_exports():
    import syncfield as sf
    # Core orchestrator API
    assert hasattr(sf, "SessionOrchestrator")
    assert hasattr(sf, "SyncToneConfig")
    assert hasattr(sf, "ChirpSpec")
    # Protocol + base class for adapter authors
    assert hasattr(sf, "Stream")
    assert hasattr(sf, "StreamBase")
    # Key types
    assert hasattr(sf, "StreamCapabilities")
    assert hasattr(sf, "SessionState")
    assert hasattr(sf, "SyncPoint")
    # Clock
    assert hasattr(sf, "SessionClock")
    # Version
    assert hasattr(sf, "__version__")


def test_testing_subpackage():
    from syncfield.testing import FakeStream
    assert FakeStream("x").id == "x"


def test_adapters_subpackage_jsonl_always_importable():
    from syncfield.adapters import JSONLFileStream
    assert JSONLFileStream is not None


def test_no_old_sync_session_export():
    import syncfield as sf
    assert not hasattr(sf, "SyncSession")
