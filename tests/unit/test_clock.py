"""Tests for SessionClock — the immutable clock handle passed to Streams."""

from __future__ import annotations

import dataclasses
import time

import pytest

from syncfield.clock import SessionClock
from syncfield.types import SyncPoint


def _make_clock(host_id: str = "host_01") -> SessionClock:
    return SessionClock(sync_point=SyncPoint.create_now(host_id))


def test_session_clock_holds_sync_point():
    sp = SyncPoint.create_now("host_01")
    clock = SessionClock(sync_point=sp)
    assert clock.sync_point is sp
    assert clock.host_id == "host_01"


def test_now_ns_is_monotonic():
    clock = _make_clock()
    t1 = clock.now_ns()
    t2 = clock.now_ns()
    assert t2 >= t1
    # Distance from the real monotonic clock should be negligible
    assert abs(t2 - time.monotonic_ns()) < 10_000_000  # 10 ms slack


def test_elapsed_ns_from_start():
    clock = _make_clock()
    time.sleep(0.005)
    elapsed = clock.elapsed_ns()
    assert elapsed >= 4_000_000  # at least 4 ms
    assert elapsed < 100_000_000  # but well under 100 ms


def test_session_clock_is_frozen_dataclass():
    clock = _make_clock()
    assert dataclasses.is_dataclass(clock)
    with pytest.raises(dataclasses.FrozenInstanceError):
        clock.sync_point = SyncPoint.create_now("other")  # type: ignore[misc]
