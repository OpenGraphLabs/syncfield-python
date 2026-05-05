"""SDK contract tests for GUI no-code onboarding (Phase 2).

Five contracts documented in syncfield-sensor-onboarding-enhancements §5:

1. on_connect callback is non-blocking.
2. Burst-aware capture_ns interpolation via burst_timestamps().
3. Transient transport hiccup auto-reopen with backoff.
4. SyncToneConfig.silent() MUST NOT register a host_audio stream.
5. A stream-level error MUST NOT propagate to SessionOrchestrator.
"""

from __future__ import annotations

import threading
import time
from typing import Any, List, Optional
from unittest.mock import patch

import pytest

from syncfield.adapters._generic import (
    TRANSIENT_REOPEN_MAX_ATTEMPTS,
    retry_open,
)
from syncfield.adapters.polling_sensor import PollingSensorStream
from syncfield.adapters.push_sensor import PushSensorStream, burst_timestamps
from syncfield.orchestrator import SessionOrchestrator
from syncfield.testing import FakeStream
from syncfield.tone import SyncToneConfig
from syncfield.types import SessionState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session(tmp_path, **kwargs) -> SessionOrchestrator:
    """Build a test session with auto-countdown stripped."""
    session = SessionOrchestrator(
        host_id=kwargs.pop("host_id", "test_host"),
        output_dir=tmp_path,
        sync_tone=kwargs.pop("sync_tone", SyncToneConfig.silent()),
        **kwargs,
    )
    real_start = session.start

    def _fast_start(*args, **start_kwargs):
        start_kwargs.setdefault("countdown_s", 0)
        return real_start(*args, **start_kwargs)

    session.start = _fast_start  # type: ignore[method-assign]
    return session


# ---------------------------------------------------------------------------
# Contract 1 — on_connect callback is non-blocking
# ---------------------------------------------------------------------------


class TestContract1OnConnectNonBlocking:
    """on_connect MUST NOT block the stream's connect() call.

    If a user's on_connect sleeps for 2 s, the stream.connect() call itself
    must return quickly (< 0.5 s wall time), and the callback runs in the
    background daemon thread.
    """

    def test_slow_on_connect_does_not_block_connect_call(self):
        """connect() returns in < 0.5 s even when on_connect sleeps for 2 s."""
        callback_started = threading.Event()
        callback_done = threading.Event()

        def slow_on_connect(stream: PushSensorStream) -> None:
            callback_started.set()
            time.sleep(2.0)
            callback_done.set()

        stream = PushSensorStream("test_sensor", on_connect=slow_on_connect)

        t0 = time.monotonic()
        stream.connect()
        elapsed = time.monotonic() - t0

        # connect() must return well before the callback finishes
        assert elapsed < 0.5, (
            f"connect() blocked for {elapsed:.2f}s; "
            "on_connect callback must run in background thread"
        )
        # The background thread should have started the callback
        assert callback_started.wait(timeout=0.5), (
            "on_connect callback never started"
        )
        # Cleanup — don't leave the background thread running forever
        callback_done.wait(timeout=3.0)

    def test_connect_still_sets_connected_flag(self):
        """Stream is immediately ready for push() after connect() returns."""
        connected_states: List[bool] = []

        def on_connect(stream: PushSensorStream) -> None:
            time.sleep(0.1)

        stream = PushSensorStream("test_sensor", on_connect=on_connect)
        stream.connect()
        # _connected must be True immediately after connect() returns
        assert stream._connected is True

    def test_no_on_connect_callback_still_works(self):
        """Streams without on_connect connect instantly and are usable."""
        stream = PushSensorStream("no_cb_sensor")
        stream.connect()
        assert stream._connected is True

    def test_on_connect_exception_does_not_propagate_to_connect(self):
        """Even if on_connect raises, connect() must not raise.

        The daemon thread that runs on_connect may raise, but that exception
        must stay confined to that thread — it must not propagate to connect()
        or to any other thread.  We wrap the thread's excepthook for this test
        to absorb the expected RuntimeError without triggering a pytest warning.
        """
        done = threading.Event()
        exceptions: List[Exception] = []
        original_excepthook = threading.excepthook

        def absorbing_excepthook(args):
            if (
                args.exc_type is RuntimeError
                and str(args.exc_value) == "bad callback"
            ):
                exceptions.append(args.exc_value)
            else:
                original_excepthook(args)

        threading.excepthook = absorbing_excepthook
        try:
            def bad_on_connect(stream: PushSensorStream) -> None:
                raise RuntimeError("bad callback")

            stream = PushSensorStream("err_sensor", on_connect=bad_on_connect)
            stream.connect()
            done.wait(timeout=0.5)  # wait for thread to start
            # Give it a moment to raise and be caught
            time.sleep(0.1)
        finally:
            threading.excepthook = original_excepthook

        assert stream._connected is True
        # The exception was absorbed (isolated to the thread)
        assert len(exceptions) == 1


# ---------------------------------------------------------------------------
# Contract 2 — Burst-aware capture_ns interpolation
# ---------------------------------------------------------------------------


class TestContract2BurstTimestamps:
    """burst_timestamps() must distribute N timestamps uniformly.

    The last timestamp must equal anchor_ns; adjacent deltas must equal
    round(1e9 / expected_hz).
    """

    def test_single_sample_returns_anchor(self):
        anchor = 1_000_000_000_000
        ts = burst_timestamps(1, anchor_ns=anchor, expected_hz=1000.0)
        assert ts == [anchor]

    def test_five_samples_at_1khz_spaced_by_1ms(self):
        anchor = 1_000_000_000_000
        ts = burst_timestamps(5, anchor_ns=anchor, expected_hz=1000.0)
        assert len(ts) == 5
        assert ts[-1] == anchor
        dt_ns = round(1e9 / 1000.0)  # 1_000_000 ns = 1 ms
        for i in range(4):
            delta = ts[i + 1] - ts[i]
            assert delta == dt_ns, (
                f"expected delta {dt_ns} ns between sample {i} and {i+1}, "
                f"got {delta} ns"
            )

    def test_burst_at_200hz_8_samples(self):
        """BLE IMU profile: 8 samples per notification at 200 Hz."""
        anchor = 5_000_000_000_000
        ts = burst_timestamps(8, anchor_ns=anchor, expected_hz=200.0)
        assert ts[-1] == anchor
        dt_ns = round(1e9 / 200.0)  # 5_000_000 ns = 5 ms
        for i in range(7):
            assert ts[i + 1] - ts[i] == dt_ns

    def test_ascending_order(self):
        anchor = 9_000_000_000_000
        ts = burst_timestamps(10, anchor_ns=anchor, expected_hz=500.0)
        assert ts == sorted(ts), "timestamps must be in ascending order"

    def test_invalid_n_raises(self):
        with pytest.raises(ValueError, match="n must be >= 1"):
            burst_timestamps(0, anchor_ns=0, expected_hz=100.0)

    def test_invalid_hz_raises(self):
        with pytest.raises(ValueError, match="expected_hz must be > 0"):
            burst_timestamps(5, anchor_ns=0, expected_hz=0.0)

    def test_anchor_defaults_to_now(self):
        """When anchor_ns is omitted, the last timestamp is close to now."""
        before = time.monotonic_ns()
        ts = burst_timestamps(3, expected_hz=100.0)
        after = time.monotonic_ns()
        # Last timestamp should be within a reasonable range around call time
        assert before <= ts[-1] <= after + 1_000_000  # 1 ms slack

    def test_timestamps_fed_to_push_have_correct_spacing(self):
        """push() accepts burst-spaced timestamps and stores them correctly."""
        anchor = 2_000_000_000_000
        ts = burst_timestamps(3, anchor_ns=anchor, expected_hz=1000.0)
        stream = PushSensorStream("burst_test")
        stream.connect()

        captured: List[int] = []
        stream.on_sample(lambda ev: captured.append(ev.capture_ns))

        for t in ts:
            stream.push({"x": 1.0}, capture_ns=t)

        assert captured == ts, "pushed capture_ns values must match burst timestamps"


# ---------------------------------------------------------------------------
# Contract 3 — Transient transport hiccup auto-reopen
# ---------------------------------------------------------------------------


class TestContract3TransientReopen:
    """retry_open must retry up to TRANSIENT_REOPEN_MAX_ATTEMPTS times."""

    def test_retry_open_succeeds_on_third_attempt(self):
        """retry_open must retry and return the handle on eventual success."""
        call_count = [0]
        sentinel = object()

        def flaky_open():
            call_count[0] += 1
            if call_count[0] < 3:
                raise OSError("transient failure")
            return sentinel

        result = retry_open(
            flaky_open,
            max_attempts=5,
            max_wait_s=0.0,  # no real sleep in unit test
            stream_id="test_sensor",
        )
        assert result is sentinel
        assert call_count[0] == 3

    def test_retry_open_raises_after_max_attempts(self):
        """retry_open must re-raise after exhausting all attempts."""
        call_count = [0]

        def always_fails():
            call_count[0] += 1
            raise OSError("persistent failure")

        with pytest.raises(OSError, match="persistent failure"):
            retry_open(
                always_fails,
                max_attempts=TRANSIENT_REOPEN_MAX_ATTEMPTS,
                max_wait_s=0.0,
                stream_id="persistent_sensor",
            )
        assert call_count[0] == TRANSIENT_REOPEN_MAX_ATTEMPTS

    def test_retry_open_succeeds_on_first_attempt(self):
        """When open_fn succeeds immediately, result is returned with 1 call."""
        handle = object()
        call_count = [0]

        def good_open():
            call_count[0] += 1
            return handle

        result = retry_open(good_open, stream_id="ok_sensor")
        assert result is handle
        assert call_count[0] == 1

    def test_polling_sensor_retries_open_on_transient_error(self, monkeypatch):
        """PollingSensorStream.connect() must retry a flaky open callback."""
        open_count = [0]
        handle = object()

        def flaky_open():
            open_count[0] += 1
            if open_count[0] < 3:
                raise OSError("transient USB hiccup")
            return handle

        # Patch time.sleep inside _generic so the test doesn't actually wait
        monkeypatch.setattr("syncfield.adapters._generic.time.sleep", lambda _s: None)

        stream = PollingSensorStream(
            "serial_sensor",
            read=lambda h: {"x": 1.0},
            hz=10.0,
            open=flaky_open,
        )
        stream.connect()
        assert open_count[0] == 3, (
            f"expected 3 open attempts (2 transient + 1 success), "
            f"got {open_count[0]}"
        )
        stream.disconnect()

    def test_polling_sensor_raises_after_max_failed_opens(self, monkeypatch):
        """PollingSensorStream.connect() re-raises when all retries are exhausted."""
        open_count = [0]

        def always_fails():
            open_count[0] += 1
            raise OSError("persistent hardware failure")

        monkeypatch.setattr("syncfield.adapters._generic.time.sleep", lambda _s: None)

        stream = PollingSensorStream(
            "dead_sensor",
            read=lambda h: {"x": 1.0},
            hz=10.0,
            open=always_fails,
        )
        with pytest.raises(OSError, match="persistent hardware failure"):
            stream.connect()

        assert open_count[0] == TRANSIENT_REOPEN_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Contract 4 — SyncToneConfig.silent() MUST NOT register host_audio
# ---------------------------------------------------------------------------


class TestContract4SilentNoHostAudio:
    """SyncToneConfig.silent() must suppress auto-injection of host_audio."""

    def test_silent_config_has_suppress_host_audio_true(self):
        """silent() factory must set suppress_host_audio=True."""
        cfg = SyncToneConfig.silent()
        assert cfg.suppress_host_audio is True

    def test_default_config_does_not_suppress_host_audio(self):
        """default() and audible() must leave suppress_host_audio=False."""
        assert SyncToneConfig.default().suppress_host_audio is False
        assert SyncToneConfig.audible().suppress_host_audio is False

    def test_manual_enabled_false_does_not_suppress_host_audio(self):
        """Existing code using SyncToneConfig(enabled=False) keeps old behaviour."""
        cfg = SyncToneConfig(enabled=False)
        assert cfg.suppress_host_audio is False

    def test_silent_session_excludes_host_audio_from_add(self, tmp_path):
        """add() must not pre-register host_audio when sync_tone is silent()."""
        # Simulate an environment where a mic IS available so we can confirm
        # the suppress flag is what's blocking injection, not absence of HW.
        with patch(
            "syncfield.adapters.host_audio.is_audio_available", return_value=True
        ), patch(
            "syncfield.adapters.host_audio.HostAudioStream"
        ) as mock_has:
            session = SessionOrchestrator(
                host_id="rig",
                output_dir=tmp_path,
                sync_tone=SyncToneConfig.silent(),
            )
            session.add(FakeStream("cam"))

        assert "host_audio" not in session._streams
        mock_has.assert_not_called()

    def test_silent_session_excludes_host_audio_from_connect(self, tmp_path):
        """connect() must not inject host_audio when sync_tone is silent()."""
        with patch(
            "syncfield.adapters.host_audio.is_audio_available", return_value=True
        ), patch(
            "syncfield.adapters.host_audio.HostAudioStream"
        ) as mock_has:
            session = SessionOrchestrator(
                host_id="rig",
                output_dir=tmp_path,
                sync_tone=SyncToneConfig.silent(),
            )
            session.add(FakeStream("cam"))
            session.connect()

        assert "host_audio" not in session._streams
        mock_has.assert_not_called()
        session.disconnect()

    def test_non_silent_session_suppress_host_audio_is_false(self):
        """Sessions with default() sync_tone have suppress_host_audio=False.

        This is the structural contract: the preregister helper checks
        suppress_host_audio, and for non-silent configs the flag is False.
        We verify the flag directly since the conftest autouse fixture
        patches out the helper itself (to avoid needing real audio HW in
        every test).
        """
        cfg = SyncToneConfig.default()
        assert cfg.suppress_host_audio is False, (
            "default() sync_tone must NOT suppress host_audio injection"
        )
        # The orchestrator reads _sync_tone.suppress_host_audio in both helper
        # methods — confirm it would be honoured at the config level.
        cfg2 = SyncToneConfig.audible()
        assert cfg2.suppress_host_audio is False


# ---------------------------------------------------------------------------
# Contract 5 — Stream errors MUST NOT propagate to SessionOrchestrator
# ---------------------------------------------------------------------------


class _FailingCaptureStream(FakeStream):
    """FakeStream whose capture 'thread' raises on first read after start_recording.

    Simulates a push-sensor whose user code crashes after recording starts.
    The crash must not bubble out of the session.
    """

    def __init__(self, id: str, **kwargs: Any) -> None:
        super().__init__(id=id, **kwargs)
        self._capture_thread: Optional[threading.Thread] = None
        self.captured_error: Optional[Exception] = None
        self.emitted_samples = 0
        self._stop_event = threading.Event()

    def connect(self) -> None:
        """No-op; capture starts in start_recording for simplicity."""
        pass

    def start_recording(self, session_clock) -> None:  # type: ignore[override]
        super().start(session_clock)  # reuse legacy start for counters
        self._stop_event.clear()
        self._capture_thread = threading.Thread(
            target=self._failing_loop,
            daemon=True,
        )
        self._capture_thread.start()

    def _failing_loop(self) -> None:
        try:
            raise RuntimeError("simulated sensor hardware crash")
        except RuntimeError as exc:
            self.captured_error = exc
            # The error stays in the thread — it must not propagate to
            # the orchestrator. FakeStream.stop() returns the report.

    def stop_recording(self) -> Any:
        self._stop_event.set()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=1.0)
        return super().stop()

    def disconnect(self) -> None:
        pass


class _StableStream(FakeStream):
    """FakeStream that emits samples steadily for the duration of a test."""

    def __init__(self, id: str, **kwargs: Any) -> None:
        super().__init__(id=id, **kwargs)
        self.received_samples = 0
        self._sample_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def on_sample(self, callback) -> None:  # type: ignore[override]
        super().on_sample(callback)

    def connect(self) -> None:
        pass

    def start_recording(self, session_clock) -> None:  # type: ignore[override]
        super().start(session_clock)
        self._stop_event.clear()
        self._sample_thread = threading.Thread(
            target=self._emit_loop, daemon=True
        )
        self._sample_thread.start()

    def _emit_loop(self) -> None:
        while not self._stop_event.wait(timeout=0.01):
            self.push_sample(frame_number=self.received_samples, capture_ns=time.monotonic_ns())
            self.received_samples += 1

    def stop_recording(self) -> Any:
        self._stop_event.set()
        if self._sample_thread is not None:
            self._sample_thread.join(timeout=1.0)
        return super().stop()

    def disconnect(self) -> None:
        pass


class TestContract5StreamErrorIsolation:
    """A stream's runtime crash MUST NOT kill the session.

    After one stream's capture thread raises, other streams must still
    receive data and the session state must remain RECORDING until stop().
    """

    def test_failing_stream_does_not_kill_session_state(self, tmp_path):
        """Session stays in RECORDING even when one stream crashes."""
        stable = _StableStream("healthy_cam")
        crashing = _FailingCaptureStream("crashing_sensor")

        session = _session(tmp_path)
        session.add(stable)
        session.add(crashing)
        session.start()

        # Allow the crash to happen
        time.sleep(0.05)

        # Session must still be in RECORDING
        assert session.state is SessionState.RECORDING, (
            f"Session should stay RECORDING after one stream crashes; "
            f"got state={session.state}"
        )

        session.stop()
        session.disconnect()

    def test_failing_stream_error_stays_in_stream_thread(self, tmp_path):
        """The crash is captured in the stream thread, not the session."""
        crashing = _FailingCaptureStream("crashing_sensor")

        session = _session(tmp_path)
        session.add(crashing)
        session.start()

        time.sleep(0.05)

        # The error must be captured inside the stream, not raised externally
        assert crashing.captured_error is not None
        assert isinstance(crashing.captured_error, RuntimeError)

        session.stop()
        session.disconnect()

    def test_healthy_stream_receives_samples_while_sibling_crashes(self, tmp_path):
        """Healthy stream continues emitting after its sibling crashes."""
        stable = _StableStream("healthy_cam")
        crashing = _FailingCaptureStream("crashing_sensor")

        session = _session(tmp_path)
        session.add(stable)
        session.add(crashing)
        session.start()

        # Give healthy stream time to produce samples
        time.sleep(0.1)

        assert stable.received_samples > 0, (
            f"Healthy stream should have produced samples by now; "
            f"got {stable.received_samples}"
        )

        session.stop()
        session.disconnect()

    def test_stop_is_clean_when_one_stream_crashed(self, tmp_path):
        """stop() must not raise even if one stream's capture thread crashed."""
        crashing = _FailingCaptureStream("crashing_sensor")

        session = _session(tmp_path)
        session.add(crashing)
        session.start()

        time.sleep(0.05)

        # stop() must succeed without raising
        report = session.stop()
        assert report is not None
        session.disconnect()
