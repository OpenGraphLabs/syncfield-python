"""Public per-stream status types and the reconnect SPI default.

These cover the additive data types introduced alongside the reconnect
supervisor: the :class:`StreamConnectionState` enum, the immutable
:class:`StreamStatus` snapshot, the ``supports_recording_reconnect``
capability flag, and :meth:`StreamBase.reconnect`'s default behaviour.
"""

from __future__ import annotations

from syncfield.stream import StreamBase
from syncfield.types import (
    StreamCapabilities,
    StreamConnectionState,
    StreamStatus,
)


class TestStreamConnectionState:
    def test_values_match_orchestrator_state_strings(self):
        # The orchestrator tracks connection state as these exact strings
        # (``_set_stream_state``); the public enum must round-trip them so a
        # status snapshot stays byte-stable with the internal bookkeeping.
        assert StreamConnectionState("idle") is StreamConnectionState.IDLE
        assert StreamConnectionState("connecting") is StreamConnectionState.CONNECTING
        assert StreamConnectionState("connected") is StreamConnectionState.CONNECTED
        assert StreamConnectionState("stalled") is StreamConnectionState.STALLED
        assert (
            StreamConnectionState("reconnecting")
            is StreamConnectionState.RECONNECTING
        )
        assert StreamConnectionState("failed") is StreamConnectionState.FAILED
        assert (
            StreamConnectionState("disconnected")
            is StreamConnectionState.DISCONNECTED
        )


class TestStreamStatus:
    def test_holds_fields_with_safe_defaults(self):
        status = StreamStatus(
            stream_id="cam_ego",
            state=StreamConnectionState.CONNECTED,
        )
        assert status.stream_id == "cam_ego"
        assert status.state is StreamConnectionState.CONNECTED
        assert status.error is None
        assert status.reconnect_attempts == 0

    def test_to_dict_serialises_state_as_its_string_value(self):
        status = StreamStatus(
            stream_id="oglo_left",
            state=StreamConnectionState.RECONNECTING,
            error="ble link lost",
            reconnect_attempts=2,
        )
        assert status.to_dict() == {
            "stream_id": "oglo_left",
            "state": "reconnecting",
            "error": "ble link lost",
            "reconnect_attempts": 2,
        }


class TestRecordingReconnectCapability:
    def test_defaults_false(self):
        # Video adapters cannot safely re-open a file mid-recording, so the
        # capability is opt-in; the default must be conservative.
        assert StreamCapabilities().supports_recording_reconnect is False

    def test_round_trips_through_to_dict(self):
        caps = StreamCapabilities(supports_recording_reconnect=True)
        assert caps.to_dict()["supports_recording_reconnect"] is True


class _ConnectCountingStream(StreamBase):
    def __init__(self) -> None:
        super().__init__(id="s", kind="custom", capabilities=StreamCapabilities())
        self.events: list[str] = []

    def connect(self) -> None:
        self.events.append("connect")

    def disconnect(self) -> None:
        self.events.append("disconnect")


class TestStreamBaseReconnectDefault:
    def test_default_reconnect_disconnects_then_connects(self):
        stream = _ConnectCountingStream()
        stream.reconnect()
        # Order matters: release the old handle before re-opening.
        assert stream.events == ["disconnect", "connect"]
