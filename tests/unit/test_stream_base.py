import dataclasses

import pytest

from syncfield.clock import SessionClock
from syncfield.stream import StreamBase
from syncfield.types import StreamCapabilities, SyncPoint


class _Dummy(StreamBase):
    def __init__(self) -> None:
        super().__init__("d", "sensor", StreamCapabilities())


def _clock(armed_ns: int | None = None) -> SessionClock:
    sp = SyncPoint.create_now(host_id="h")
    return SessionClock(sync_point=sp, recording_armed_ns=armed_ns)


def test_anchor_helper_returns_none_before_first_frame():
    d = _Dummy()
    d._begin_recording_window(_clock(armed_ns=100))
    assert d._recording_anchor() is None


def test_anchor_helper_captures_first_frame_then_ignores_later():
    d = _Dummy()
    d._begin_recording_window(_clock(armed_ns=100))
    d._observe_first_frame(host_ns=250, device_ns=9_000)
    d._observe_first_frame(host_ns=300, device_ns=10_000)  # ignored
    anchor = d._recording_anchor()
    assert anchor is not None
    assert anchor.armed_host_ns == 100
    assert anchor.first_frame_host_ns == 250
    assert anchor.first_frame_device_ns == 9_000


def test_anchor_helper_without_device_ts():
    d = _Dummy()
    d._begin_recording_window(_clock(armed_ns=100))
    d._observe_first_frame(host_ns=250, device_ns=None)
    anchor = d._recording_anchor()
    assert anchor is not None
    assert anchor.first_frame_device_ns is None


def test_anchor_helper_noop_if_armed_ns_missing():
    d = _Dummy()
    d._begin_recording_window(_clock(armed_ns=None))
    d._observe_first_frame(host_ns=250, device_ns=None)
    assert d._recording_anchor() is None


def test_anchor_helper_reset_on_second_recording_window():
    d = _Dummy()
    d._begin_recording_window(_clock(armed_ns=100))
    d._observe_first_frame(host_ns=250, device_ns=9_000)
    d._begin_recording_window(_clock(armed_ns=1_000))
    assert d._recording_anchor() is None  # reset on new window
    d._observe_first_frame(host_ns=1_100, device_ns=500)
    anchor = d._recording_anchor()
    assert anchor is not None
    assert anchor.armed_host_ns == 1_000
    assert anchor.first_frame_host_ns == 1_100
