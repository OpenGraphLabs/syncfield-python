"""Tests for :class:`ChirpEmission` and the hardware-timestamped
:class:`SoundDeviceChirpPlayer` that produces it.

The player tests use a fake ``sounddevice`` module patched into
``sys.modules`` so the real PortAudio backend is never touched. The
fake :class:`_FakeOutputStream` records the callback the player
registers and lets the test thread drive it synchronously with a
scripted ``time_info`` struct, exercising both the hardware-timestamp
path and the software fallback.
"""

from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, List

import pytest


# ---------------------------------------------------------------------------
# ChirpEmission value type
# ---------------------------------------------------------------------------


class TestChirpEmission:
    def test_hardware_source_exposes_hardware_ns_as_best(self):
        from syncfield.types import ChirpEmission

        e = ChirpEmission(software_ns=100, hardware_ns=150, source="hardware")
        assert e.best_ns == 150

    def test_software_fallback_uses_software_ns_as_best(self):
        from syncfield.types import ChirpEmission

        e = ChirpEmission(
            software_ns=100, hardware_ns=None, source="software_fallback"
        )
        assert e.best_ns == 100

    def test_silent_source_uses_software_ns_as_best(self):
        from syncfield.types import ChirpEmission

        e = ChirpEmission(software_ns=100, hardware_ns=None, source="silent")
        assert e.best_ns == 100

    def test_to_dict_includes_hardware_when_present(self):
        from syncfield.types import ChirpEmission

        e = ChirpEmission(software_ns=100, hardware_ns=150, source="hardware")
        assert e.to_dict() == {
            "software_ns": 100,
            "hardware_ns": 150,
            "source": "hardware",
        }

    def test_to_dict_omits_hardware_ns_when_none(self):
        from syncfield.types import ChirpEmission

        e = ChirpEmission(software_ns=100, hardware_ns=None, source="silent")
        d = e.to_dict()
        assert "hardware_ns" not in d
        assert d == {"software_ns": 100, "source": "silent"}

    def test_invalid_source_rejected(self):
        from syncfield.types import ChirpEmission

        with pytest.raises(ValueError, match="source"):
            ChirpEmission(
                software_ns=100, hardware_ns=None, source="bogus"  # type: ignore[arg-type]
            )

    def test_is_frozen(self):
        import dataclasses

        from syncfield.types import ChirpEmission

        e = ChirpEmission(software_ns=1, hardware_ns=None, source="silent")
        with pytest.raises(dataclasses.FrozenInstanceError):
            e.software_ns = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Fake sounddevice backend for player tests
# ---------------------------------------------------------------------------


class _CallbackStop(Exception):
    """Stand-in for ``sounddevice.CallbackStop``."""


class _FakeStatus:
    def __bool__(self) -> bool:
        return False


class _FakeOutputStream:
    """Captures the callback registered by SoundDeviceChirpPlayer.

    Tests drive playback by calling :meth:`fire_callback` with a scripted
    ``time_info`` struct — this mimics the real PortAudio callback chain
    without actually touching hardware.
    """

    instances: List["_FakeOutputStream"] = []

    def __init__(
        self,
        samplerate: int,
        channels: int,
        callback: Any,
        finished_callback: Any = None,
        **_: Any,
    ) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.callback = callback
        self.finished_callback = finished_callback
        self.started = False
        self.closed = False
        _FakeOutputStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def fire_callback(self, frames: int, out_buffer: Any, time_info: Any) -> bool:
        """Invoke the registered callback once.

        Returns ``True`` if the callback raised :class:`_CallbackStop`
        (meaning playback finished); ``False`` otherwise.
        """
        try:
            self.callback(out_buffer, frames, time_info, _FakeStatus())
        except _CallbackStop:
            if self.finished_callback is not None:
                self.finished_callback()
            return True
        return False


@pytest.fixture
def fake_sounddevice(monkeypatch):
    """Install a fake ``sounddevice`` module into ``sys.modules``."""
    _FakeOutputStream.instances.clear()
    fake_sd = SimpleNamespace(
        OutputStream=_FakeOutputStream,
        CallbackStop=_CallbackStop,
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    yield fake_sd
    _FakeOutputStream.instances.clear()


def _wait_for_stream_started(timeout_sec: float = 0.5) -> _FakeOutputStream:
    """Poll until a `_FakeOutputStream` has been started by the player."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if _FakeOutputStream.instances and _FakeOutputStream.instances[-1].started:
            return _FakeOutputStream.instances[-1]
        time.sleep(0.005)
    raise AssertionError("no OutputStream started within timeout")


# ---------------------------------------------------------------------------
# Hardware timestamp capture path
# ---------------------------------------------------------------------------


class TestSoundDeviceChirpPlayerHardwareTimestamp:
    def test_hardware_timestamp_captured_from_dac_time(self, fake_sounddevice):
        import numpy as np

        from syncfield import tone

        player = tone.SoundDeviceChirpPlayer(sample_rate=16000)
        spec = tone.ChirpSpec(400, 2500, 10, 0.8, 2)

        def drive_stream():
            stream = _wait_for_stream_started()
            out_buffer = np.zeros((64, 1), dtype=np.float32)
            # currentTime=100.0, outputBufferDacTime=100.01 → 10ms latency
            time_info = SimpleNamespace(
                currentTime=100.0,
                outputBufferDacTime=100.01,
                inputBufferAdcTime=0.0,
            )
            stream.fire_callback(frames=64, out_buffer=out_buffer, time_info=time_info)

        t = threading.Thread(target=drive_stream, daemon=True)
        t.start()
        emission = player.play(spec)
        t.join(timeout=1.0)

        assert emission.source == "hardware"
        assert emission.hardware_ns is not None
        delta_ms = (emission.hardware_ns - emission.software_ns) / 1_000_000
        # DAC is 10 ms in the future; allow wide tolerance to absorb the
        # unavoidable Python thread-scheduling jitter between
        # ``software_ns`` capture and the fake callback invocation.
        assert 5 <= delta_ms <= 50, f"hw offset {delta_ms:.3f} ms out of band"

    def test_software_fallback_when_dac_time_missing(self, fake_sounddevice):
        import numpy as np

        from syncfield import tone

        player = tone.SoundDeviceChirpPlayer(sample_rate=16000)
        spec = tone.ChirpSpec(400, 2500, 10, 0.8, 2)

        def drive_stream():
            stream = _wait_for_stream_started()
            out_buffer = np.zeros((64, 1), dtype=np.float32)
            # currentTime == outputBufferDacTime → no latency info
            time_info = SimpleNamespace(
                currentTime=50.0,
                outputBufferDacTime=50.0,
                inputBufferAdcTime=0.0,
            )
            stream.fire_callback(frames=64, out_buffer=out_buffer, time_info=time_info)

        t = threading.Thread(target=drive_stream, daemon=True)
        t.start()
        emission = player.play(spec)
        t.join(timeout=1.0)

        assert emission.source == "software_fallback"
        assert emission.hardware_ns is None
        assert emission.software_ns > 0

    def test_software_fallback_when_first_callback_times_out(self, fake_sounddevice):
        from syncfield import tone

        player = tone.SoundDeviceChirpPlayer(sample_rate=16000)
        # Shrink timeout so the test stays fast; nothing drives the callback
        player._first_callback_timeout = 0.02  # type: ignore[attr-defined]
        spec = tone.ChirpSpec(400, 2500, 10, 0.8, 2)

        emission = player.play(spec)
        assert emission.source == "software_fallback"
        assert emission.hardware_ns is None

    def test_playback_exhausts_buffer_and_fires_finished_callback(
        self, fake_sounddevice
    ):
        import numpy as np

        from syncfield import tone

        player = tone.SoundDeviceChirpPlayer(sample_rate=16000)
        # 10 ms chirp at 16 kHz → 160 samples
        spec = tone.ChirpSpec(400, 2500, 10, 0.8, 2)

        finished_fired = threading.Event()

        def drive_stream():
            stream = _wait_for_stream_started()
            # Hijack the finished_callback so the test can observe it
            original = stream.finished_callback

            def hook():
                if original is not None:
                    original()
                finished_fired.set()

            stream.finished_callback = hook

            # First callback: 64 samples with HW time info
            out1 = np.zeros((64, 1), dtype=np.float32)
            ti = SimpleNamespace(
                currentTime=0.0, outputBufferDacTime=0.001, inputBufferAdcTime=0.0
            )
            assert stream.fire_callback(64, out1, ti) is False
            # Second callback: 64 more samples
            out2 = np.zeros((64, 1), dtype=np.float32)
            assert stream.fire_callback(64, out2, ti) is False
            # Third callback: drains the last 32 → CallbackStop
            out3 = np.zeros((64, 1), dtype=np.float32)
            assert stream.fire_callback(64, out3, ti) is True

        t = threading.Thread(target=drive_stream, daemon=True)
        t.start()
        emission = player.play(spec)
        t.join(timeout=1.0)

        assert emission.source == "hardware"
        assert finished_fired.wait(1.0) is True

    def test_close_drops_active_streams(self, fake_sounddevice):
        from syncfield import tone

        player = tone.SoundDeviceChirpPlayer(sample_rate=16000)
        player._first_callback_timeout = 0.02  # type: ignore[attr-defined]
        player.play(tone.ChirpSpec(400, 2500, 10, 0.8, 2))
        # Stream is still in active list (callback never fired to completion)
        assert len(_FakeOutputStream.instances) == 1
        player.close()
        assert _FakeOutputStream.instances[-1].closed is True


# ---------------------------------------------------------------------------
# Silent player returns a well-formed ChirpEmission
# ---------------------------------------------------------------------------


class TestSilentChirpPlayerEmission:
    def test_silent_player_returns_silent_emission(self):
        from syncfield.tone import SilentChirpPlayer
        from syncfield.types import ChirpSpec

        emission = SilentChirpPlayer().play(ChirpSpec(400, 2500, 10, 0.8, 2))
        assert emission.source == "silent"
        assert emission.hardware_ns is None
        assert emission.software_ns > 0
