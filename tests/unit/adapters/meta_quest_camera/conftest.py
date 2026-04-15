"""Shared fixtures for MetaQuestCameraStream unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def quest_host() -> str:
    return "192.0.2.10"  # RFC 5737 TEST-NET-1, never routable


@pytest.fixture
def quest_port() -> int:
    return 14045
