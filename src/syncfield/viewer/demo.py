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
    correctly without any mocking on the viewer side.
    """

    def __init__(
        self,
        id: str,
        width: int = 640,
        height: int = 360,
        fps: float = 30.0,
        hue_shift: float = 0.0,
    ) -> None:
        super().__init__(
            id=id,
            kind="video",
            capabilities=StreamCapabilities(
                provides_audio_track=False,
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
        self._frame_count = 0
        self._first_at: int | None = None
        self._last_at: int | None = None
        self._latest_frame: Any = None
        self._frame_lock = threading.Lock()

    def prepare(self) -> None:
        pass

    def start(self, session_clock) -> None:  # type: ignore[override]
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._generate_loop, name=f"synth-vid-{self.id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> FinalizationReport:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
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

    @property
    def latest_frame(self) -> Any:
        with self._frame_lock:
            return self._latest_frame

    def _generate_loop(self) -> None:
        """Procedurally generate a colorful moving gradient.

        The frame is a smooth sinusoidal pattern that drifts across the
        image — visually distinctive enough that screenshots show real
        motion but cheap enough to compute at 30 fps.
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
    """Fake 9-DOF IMU that produces smooth sinusoidal channels at 100 Hz."""

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
        self._frame_count = 0
        self._first_at: int | None = None
        self._last_at: int | None = None

    def prepare(self) -> None:
        pass

    def start(self, session_clock) -> None:  # type: ignore[override]
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"synth-imu-{self.id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> FinalizationReport:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
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

    def _loop(self) -> None:
        period = 0.01  # 100 Hz
        t0 = time.monotonic()
        while not self._stop.is_set():
            t = time.monotonic() - t0
            capture_ns = time.monotonic_ns()
            if self._first_at is None:
                self._first_at = capture_ns
            self._last_at = capture_ns
            self._frame_count += 1
            channels = {
                "ax": math.sin(t * 1.3) * 0.8 + math.sin(t * 7.0) * 0.1,
                "ay": math.cos(t * 1.6) * 0.6,
                "az": 9.81 + math.sin(t * 0.5) * 0.2,
                "gx": math.sin(t * 2.0) * 0.4,
                "gy": math.cos(t * 2.3) * 0.5,
                "gz": math.sin(t * 3.1) * 0.3,
            }
            self._emit_sample(
                SampleEvent(
                    stream_id=self.id,
                    frame_number=self._frame_count - 1,
                    capture_ns=capture_ns,
                    channels=channels,
                )
            )

            # Sprinkle in a health event occasionally so the health table
            # actually has content in screenshots.
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
    """Construct a realistic multi-stream session for the viewer demo."""
    session = sf.SessionOrchestrator(
        host_id="demo_rig",
        output_dir=output_dir,
        sync_tone=sf.SyncToneConfig.silent(),  # don't actually play audio in the demo
    )
    session.add(SyntheticVideoStream("cam_ego", width=640, height=360, hue_shift=0.0))
    session.add(
        SyntheticVideoStream("cam_wrist_left", width=480, height=480, hue_shift=1.7)
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
    args = parser.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    session = build_demo_session(args.output_dir)

    import syncfield.viewer as viewer

    if args.auto_record:
        # Start the session immediately so screenshots look populated.
        def _auto_record() -> None:
            time.sleep(0.5)
            try:
                session.start()
            except Exception as exc:
                print(f"auto-record failed: {exc}", file=sys.stderr)

        threading.Thread(target=_auto_record, daemon=True).start()

    if args.duration > 0:
        # Auto-close mode: run the viewer on the main thread for the
        # requested duration, then stop. The viewer's event loop doesn't
        # block on os.exit, so we set a timer that calls dpg.stop_dearpygui.
        import dearpygui.dearpygui as dpg

        def _timer() -> None:
            time.sleep(args.duration)
            try:
                dpg.stop_dearpygui()
            except Exception:
                pass

        threading.Thread(target=_timer, daemon=True).start()

    viewer.launch(session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
