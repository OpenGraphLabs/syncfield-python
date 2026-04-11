"""Headless-safe demo for the SyncField desktop viewer.

Run with::

    python -m syncfield.viewer.demo

Spins up a :class:`SessionOrchestrator` wired to a realistic mix of fake
streams (two synthetic video sources, one IMU with a BNO-style signal,
one JSONL-ish logger, and a custom sensor) so the viewer has plausible
data to render without any hardware connected.

This module doubles as the screenshot harness — it accepts
``--snapshot path.png`` to quit the viewer after a warmup period and
save the window bitmap to disk.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, List, Optional

import numpy as np

import syncfield as sf
from syncfield.stream import StreamBase
from syncfield.testing import FakeStream
from syncfield.types import (
    FinalizationReport,
    HealthEvent,
    HealthEventKind,
    SampleEvent,
    StreamCapabilities,
)


# ---------------------------------------------------------------------------
# Fake video stream — generates a moving gradient so screenshots look "live"
# ---------------------------------------------------------------------------


class SyntheticVideoStream(StreamBase):
    """Fake video source that generates a procedural gradient every ~33 ms.

    Exposes ``latest_frame`` the same way :class:`UVCWebcamStream` and
    :class:`OakCameraStream` do, so the viewer's video card renders it
    correctly without any mocking on the viewer side. Implements the
    full 4-phase lifecycle: the gradient loop runs during ``CONNECTED``
    so preview is live *before* Record is pressed, and ``_recording``
    gates the ``SampleEvent`` emission so only frames captured while
    the session is in ``RECORDING`` count toward the finalization.
    """

    def __init__(
        self,
        id: str,
        width: int = 640,
        height: int = 360,
        fps: float = 30.0,
        hue_shift: float = 0.0,
        provides_audio_track: bool = False,
    ) -> None:
        super().__init__(
            id=id,
            kind="video",
            capabilities=StreamCapabilities(
                provides_audio_track=provides_audio_track,
                supports_precise_timestamps=True,
                is_removable=False,
                produces_file=True,
            ),
        )
        self._width = width
        self._height = height
        self._period_s = 1.0 / fps
        self._hue_shift = hue_shift
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # `_recording` toggles whether the capture loop counts frames
        # and emits sample events. While False the loop still produces
        # `latest_frame` so the viewer has something to show.
        self._recording = False
        self._frame_count = 0
        self._first_at: int | None = None
        self._last_at: int | None = None
        self._latest_frame: Any = None
        self._frame_lock = threading.Lock()

    # -- 4-phase lifecycle --------------------------------------------------

    def connect(self) -> None:
        """Spawn the capture loop so preview frames are available."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._recording = False
        self._frame_count = 0
        self._first_at = None
        self._last_at = None
        self._thread = threading.Thread(
            target=self._generate_loop, name=f"synth-vid-{self.id}", daemon=True
        )
        self._thread.start()

    def start_recording(self, session_clock) -> None:  # type: ignore[override]
        """Flip the recording flag — atomic and fast."""
        self._recording = True

    def stop_recording(self) -> FinalizationReport:
        """Stop emitting samples but leave the capture loop running."""
        self._recording = False
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=self._frame_count,
            file_path=None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=None,
        )

    def disconnect(self) -> None:
        """Tear down the capture loop."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- Legacy one-shot compatibility -------------------------------------

    def prepare(self) -> None:
        pass

    def start(self, session_clock) -> None:  # type: ignore[override]
        # Compatibility path for any caller still using the legacy
        # one-shot lifecycle: connect + start_recording in one call.
        self.connect()
        self.start_recording(session_clock)

    def stop(self) -> FinalizationReport:
        report = self.stop_recording()
        self.disconnect()
        return report

    # -- Viewer integration ------------------------------------------------

    @property
    def latest_frame(self) -> Any:
        with self._frame_lock:
            return self._latest_frame

    def _generate_loop(self) -> None:
        """Procedurally generate a colorful moving gradient.

        The frame is a smooth sinusoidal pattern that drifts across the
        image — visually distinctive enough that screenshots show real
        motion but cheap enough to compute at 30 fps. Runs continuously
        while connected; sample events are only emitted during
        ``_recording`` so the frame count reflects recorded frames only.
        """
        xs = np.linspace(0, 2 * math.pi, self._width, dtype=np.float32)
        ys = np.linspace(0, 2 * math.pi, self._height, dtype=np.float32)
        xx, yy = np.meshgrid(xs, ys)
        t0 = time.monotonic()
        frame_number = 0
        while not self._stop.is_set():
            t = time.monotonic() - t0
            # Smooth blue/indigo gradient with a subtle wave for motion
            r = 0.55 + 0.30 * np.sin(xx + t * 1.2 + self._hue_shift)
            g = 0.55 + 0.30 * np.sin(yy + t * 0.9 + self._hue_shift + 2.0)
            b = 0.75 + 0.20 * np.sin(xx + yy + t * 1.5 + self._hue_shift + 4.0)
            rgb = np.stack([r, g, b], axis=-1)
            rgb = np.clip(rgb, 0.0, 1.0)
            bgr = (rgb[:, :, ::-1] * 255).astype(np.uint8)

            capture_ns = time.monotonic_ns()
            with self._frame_lock:
                self._latest_frame = bgr

            # Preview-only frames never touch the counters or the sample
            # stream — they just update `latest_frame`. Recording frames
            # do both.
            if self._recording:
                if self._first_at is None:
                    self._first_at = capture_ns
                self._last_at = capture_ns
                self._frame_count += 1
                self._emit_sample(
                    SampleEvent(
                        stream_id=self.id,
                        frame_number=frame_number,
                        capture_ns=capture_ns,
                    )
                )
                frame_number += 1
            self._stop.wait(self._period_s)


# ---------------------------------------------------------------------------
# Fake IMU — emits a sine/cosine signal as a "BNO085-style" stream
# ---------------------------------------------------------------------------


class SyntheticImuStream(StreamBase):
    """Fake 9-DOF IMU that produces smooth sinusoidal channels at 100 Hz.

    Like :class:`SyntheticVideoStream`, the sample loop runs during
    ``CONNECTED`` so the viewer can plot live values before Record is
    pressed. Only frames captured while ``_recording`` is ``True`` are
    counted into the finalization report and emitted as
    :class:`SampleEvent`.
    """

    def __init__(self, id: str) -> None:
        super().__init__(
            id=id,
            kind="sensor",
            capabilities=StreamCapabilities(
                provides_audio_track=False,
                supports_precise_timestamps=True,
                is_removable=True,
                produces_file=False,
            ),
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._recording = False
        self._frame_count = 0
        self._first_at: int | None = None
        self._last_at: int | None = None

    # -- 4-phase lifecycle -------------------------------------------------

    def connect(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._recording = False
        self._frame_count = 0
        self._first_at = None
        self._last_at = None
        self._thread = threading.Thread(
            target=self._loop, name=f"synth-imu-{self.id}", daemon=True
        )
        self._thread.start()

    def start_recording(self, session_clock) -> None:  # type: ignore[override]
        self._recording = True

    def stop_recording(self) -> FinalizationReport:
        self._recording = False
        return FinalizationReport(
            stream_id=self.id,
            status="completed",
            frame_count=self._frame_count,
            file_path=None,
            first_sample_at_ns=self._first_at,
            last_sample_at_ns=self._last_at,
            health_events=list(self._collected_health),
            error=None,
        )

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- Legacy one-shot compatibility -------------------------------------

    def prepare(self) -> None:
        pass

    def start(self, session_clock) -> None:  # type: ignore[override]
        self.connect()
        self.start_recording(session_clock)

    def stop(self) -> FinalizationReport:
        report = self.stop_recording()
        self.disconnect()
        return report

    # -- Capture loop ------------------------------------------------------

    def _loop(self) -> None:
        period = 0.01  # 100 Hz
        t0 = time.monotonic()
        while not self._stop.is_set():
            t = time.monotonic() - t0
            capture_ns = time.monotonic_ns()
            channels = {
                "ax": math.sin(t * 1.3) * 0.8 + math.sin(t * 7.0) * 0.1,
                "ay": math.cos(t * 1.6) * 0.6,
                "az": 9.81 + math.sin(t * 0.5) * 0.2,
                "gx": math.sin(t * 2.0) * 0.4,
                "gy": math.cos(t * 2.3) * 0.5,
                "gz": math.sin(t * 3.1) * 0.3,
            }

            if self._recording:
                if self._first_at is None:
                    self._first_at = capture_ns
                self._last_at = capture_ns
                self._frame_count += 1
                self._emit_sample(
                    SampleEvent(
                        stream_id=self.id,
                        frame_number=self._frame_count - 1,
                        capture_ns=capture_ns,
                        channels=channels,
                    )
                )

                # Sprinkle in a health event occasionally so the health
                # table actually has content in screenshots.
                if self._frame_count == 150:
                    self._emit_health(
                        HealthEvent(
                            stream_id=self.id,
                            kind=HealthEventKind.WARNING,
                            at_ns=capture_ns,
                            detail="synthetic jitter above threshold",
                        )
                    )
                if self._frame_count == 320:
                    self._emit_health(
                        HealthEvent(
                            stream_id=self.id,
                            kind=HealthEventKind.RECONNECT,
                            at_ns=capture_ns,
                            detail=None,
                        )
                    )

            self._stop.wait(period)


# ---------------------------------------------------------------------------
# Demo session builder
# ---------------------------------------------------------------------------


def build_demo_session(output_dir: Path) -> sf.SessionOrchestrator:
    """Construct a realistic multi-stream session for the viewer demo.

    Chirp is **enabled** with the egonaut production defaults so the viewer
    shows the real "sync tone active" UI state. A :class:`SilentChirpPlayer`
    is injected so the demo never actually emits audio — great for running
    the demo on a laptop or for capturing docs screenshots without beeping.
    """
    from syncfield.tone import SilentChirpPlayer

    session = sf.SessionOrchestrator(
        host_id="demo_rig",
        output_dir=output_dir,
        sync_tone=sf.SyncToneConfig.default(),   # chirp enabled by default
        chirp_player=SilentChirpPlayer(),         # ...but don't actually beep
    )
    # Mark at least one stream as audio-capable so the orchestrator decides
    # chirp is eligible and fills in chirp_start_ns / chirp_stop_ns in the
    # sync point — without that, the viewer's "chirp" line would read
    # "pending" forever.
    session.add(
        SyntheticVideoStream(
            "cam_ego", width=640, height=360, hue_shift=0.0,
            provides_audio_track=True,
        )
    )
    session.add(
        SyntheticVideoStream(
            "cam_wrist_left", width=480, height=480, hue_shift=1.7,
            provides_audio_track=False,
        )
    )
    session.add(SyntheticImuStream("torso_imu"))
    session.add(FakeStream("tactile_left", provides_audio_track=False))
    return session


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SyncField viewer demo")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./demo_session"),
        help="Output directory for the synthetic session.",
    )
    parser.add_argument(
        "--auto-record",
        action="store_true",
        help="Automatically click Record on startup.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help=(
            "If > 0, run for this many seconds then auto-close. "
            "Useful for screenshotting."
        ),
    )
    parser.add_argument(
        "--empty-session",
        action="store_true",
        help=(
            "Skip the synthetic streams and open with an empty session — "
            "useful for capturing the 'click Discover to begin' state."
        ),
    )
    parser.add_argument(
        "--open-discovery",
        action="store_true",
        help=(
            "After startup, automatically click the 'Discover devices' "
            "header button so screenshots capture the discovery modal."
        ),
    )
    parser.add_argument(
        "--screenshot",
        type=Path,
        default=None,
        help=(
            "Path to save a PNG screenshot of the viewer. Implies "
            "--auto-record. The viewer runs for --duration seconds, waits "
            "until the streams have warmed up, then captures the viewport "
            "via dpg.output_frame_buffer() and exits."
        ),
    )
    args = parser.parse_args(argv)

    if args.screenshot is not None and args.duration <= 0:
        # A screenshot run needs a bounded duration; default to 3s.
        args.duration = 3.0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.empty_session:
        # Bare session with no pre-populated streams — used to capture
        # the "click Discover to begin" screenshot.
        import syncfield.adapters  # noqa: F401 (register discoverers)

        from syncfield.tone import SilentChirpPlayer

        session = sf.SessionOrchestrator(
            host_id="demo_rig",
            output_dir=args.output_dir,
            sync_tone=sf.SyncToneConfig.default(),
            chirp_player=SilentChirpPlayer(),
        )
    else:
        session = build_demo_session(args.output_dir)

    if args.auto_record:
        # Start the session immediately so screenshots look populated.
        def _auto_record() -> None:
            time.sleep(0.5)
            try:
                session.start()
            except Exception as exc:
                print(f"auto-record failed: {exc}", file=sys.stderr)

        threading.Thread(target=_auto_record, daemon=True).start()

    if args.duration > 0 or args.screenshot is not None:
        import dearpygui.dearpygui as dpg
        import subprocess

        def _capture_window_screenshot() -> None:
            """Capture the viewer window to a PNG via ``screencapture``.

            The demo is launched from a terminal, so by the time the
            timer fires the frontmost app may still be the shell or the
            editor that kicked off the run — not the DPG window. Before
            capturing we ask the current Python process (which owns the
            viewer window) to activate itself via AppleScript. That
            guarantees the window is on top of whatever was previously
            frontmost, so the region capture at ``(60, 60)`` lines up
            with the pinned viewport.
            """
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)

            # Step 1: force the SyncField window to the front. We look
            # up "python" (which owns this process) and send an 'activate'
            # event. Falls through silently if System Events is
            # unreachable — the capture will still run, it just may
            # grab whatever is on top of the viewer.
            activate_script = """
                tell application "System Events"
                    set pyProcs to every application process whose unix id is %d
                    if (count of pyProcs) > 0 then
                        set frontmost of (item 1 of pyProcs) to true
                    end if
                end tell
            """ % os.getpid()
            try:
                subprocess.run(
                    ["osascript", "-e", activate_script],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except Exception as exc:
                print(f"viewer activate failed: {exc}", file=sys.stderr)

            # Small settle so the window manager finishes raising the
            # viewer above whatever was in front of it before.
            time.sleep(0.3)

            # Step 2: log the frontmost window title for debugging.
            probe = """
                tell application "System Events"
                    set frontApp to first application process whose frontmost is true
                    set frontWin to window 1 of frontApp
                    return value of attribute "AXTitle" of frontWin
                end tell
            """
            try:
                result = subprocess.run(
                    ["osascript", "-e", probe],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                print(
                    f"frontmost window title: {result.stdout.strip()!r}",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(f"title probe failed: {exc}", file=sys.stderr)

            # Step 2: capture the full screen then we can inspect it. If the
            # full-screen dump looks right we'll refine to a window capture.
            full_path = args.screenshot.with_suffix(".full.png")
            try:
                subprocess.run(
                    ["screencapture", "-x", "-t", "png", str(full_path)],
                    check=True,
                    timeout=5,
                )
                print(f"full-screen dump → {full_path}", file=sys.stderr)
            except Exception as exc:
                print(f"full-screen capture failed: {exc}", file=sys.stderr)

            # Step 3: also try the interactive window capture by sending a
            # key-like instruction to screencapture's -W mode. That's not
            # scriptable; fall back to capturing a point-sized region.
            try:
                from syncfield.viewer import theme

                # The viewer is pinned at (60, 60) by the screenshot
                # harness. macOS window chrome adds a ~28 px title bar
                # above the DPG content — we pad the capture region by
                # the same amount at the bottom to guarantee the full
                # content area is visible even after the viewport grows.
                _TITLE_BAR_PX = 28
                x, y = 60, 60
                w = theme.VIEWPORT_WIDTH
                h = theme.VIEWPORT_HEIGHT + _TITLE_BAR_PX
                subprocess.run(
                    [
                        "screencapture",
                        "-x",
                        "-t",
                        "png",
                        "-R",
                        f"{x},{y},{w},{h}",
                        str(args.screenshot),
                    ],
                    check=True,
                    timeout=5,
                )
                print(f"region dump → {args.screenshot}", file=sys.stderr)
            except Exception as exc:
                print(f"region capture failed: {exc}", file=sys.stderr)

        def _timer() -> None:
            # Extra settling time — DPG viewport move is async and the
            # first few frames can show a flash of the default dark theme.
            time.sleep(args.duration)
            if args.screenshot is not None:
                _capture_window_screenshot()
            try:
                dpg.stop_dearpygui()
            except Exception:
                pass

        threading.Thread(target=_timer, daemon=True).start()

    # Pin the viewport so the screenshot helper knows where to look.
    pin_pos = (60, 60) if args.screenshot is not None else None

    from syncfield.viewer.app import ViewerApp

    app = ViewerApp(session, title="SyncField", viewport_pos=pin_pos)

    # Optional: programmatically open the discovery modal a moment after
    # startup so screenshots can capture it without the user clicking.
    if args.open_discovery:
        def _auto_open_modal() -> None:
            time.sleep(0.8)
            try:
                if app._layout and app._layout._discovery_modal is not None:  # noqa: SLF001
                    app._layout._discovery_modal.open()  # noqa: SLF001
            except Exception as exc:
                print(f"auto-open-discovery failed: {exc}", file=sys.stderr)

        threading.Thread(target=_auto_open_modal, daemon=True).start()

    try:
        app.setup()
        app.run()
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
