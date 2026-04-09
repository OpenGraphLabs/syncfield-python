"""iPhone + Mac Webcam — dual OpenCV recording through SyncField.

Records two video streams at once — the Mac's built-in webcam and an
iPhone connected over Continuity Camera — and opens the SyncField
desktop viewer so you can click Record, watch both previews, and
click Stop. Both cameras are captured through the same
:class:`~syncfield.adapters.UVCWebcamStream` adapter because macOS
exposes the iPhone as an ordinary UVC device once Continuity Camera
is active.

Why this example exists
-----------------------
It's the shortest end-to-end SyncField recipe: one SessionOrchestrator,
two off-the-shelf adapters, one viewer launch. Use it as the template
for your own multi-camera rig and gradually swap / add streams as you
scale up (OAK-D, BLE IMU, tactile, multi-host ...).

Hardware checklist
------------------
1. Mac with a working built-in webcam (or any USB webcam at index 0).
2. iPhone signed in to the same Apple ID as the Mac, Bluetooth on,
   Continuity Camera enabled (``System Settings → General → AirPlay
   & Handoff → Continuity Camera``).
3. iPhone within Bluetooth range of the Mac.
4. Both devices ideally on wall power — Continuity occasionally
   disconnects mid-session on battery.

Install
-------
::

    pip install "syncfield[uvc,audio,viewer]"

Run
---
::

    # Default: webcam at index 0, iPhone at index 1
    python record.py

    # Custom indices / output dir / geometry
    python record.py --webcam-index 0 --iphone-index 1 \\
                     --output-dir ./my_recording \\
                     --width 1920 --height 1080 --fps 30

    # Sanity-check which OpenCV index is which camera (before running)
    python record.py --probe

Output
------
After you click **Record** then **Stop** in the viewer, the output
directory looks like::

    output/
    ├── mac_webcam.mp4                  # Mac built-in webcam video
    ├── mac_webcam.timestamps.jsonl     # Per-frame capture timestamps
    ├── iphone.mp4                      # iPhone Continuity camera video
    ├── iphone.timestamps.jsonl
    ├── sync_point.json                 # Session anchor + chirp info
    ├── manifest.json                   # Per-stream metadata
    └── session_log.jsonl               # Crash-safe timeline log

The two ``*.timestamps.jsonl`` files and ``sync_point.json`` are the
artifacts the SyncField sync service consumes for post-hoc frame-level
alignment across the two cameras.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import syncfield as sf
from syncfield.adapters import UVCWebcamStream


def build_session(
    *,
    output_dir: Path,
    webcam_index: int,
    iphone_index: int,
    width: int,
    height: int,
    fps: float,
) -> sf.SessionOrchestrator:
    """Construct a :class:`~syncfield.SessionOrchestrator` with two cameras.

    Both cameras use the same :class:`UVCWebcamStream` adapter because
    macOS surfaces the iPhone Continuity Camera as a standard UVC
    device. The only thing that differs is the ``device_index`` — on
    most Macs the built-in webcam lands at ``0`` and the iPhone at
    ``1`` once Continuity is active. Use ``--probe`` to verify.

    The chirp is enabled on the orchestrator but **will be skipped**
    for this setup because neither OpenCV camera declares an audio
    track (``provides_audio_track=False``). That's fine for a
    single-host example; the chirp is a multi-host acoustic anchor
    and isn't needed when only one host is recording. When you later
    add a host with a microphone (e.g. by registering a separate
    audio stream or moving to the multi-host examples), the chirp
    will start playing automatically.
    """
    session = sf.SessionOrchestrator(
        host_id="mac_studio",
        output_dir=output_dir,
        sync_tone=sf.SyncToneConfig.default(),  # enabled, auto-skipped here
    )

    # Mac built-in webcam (or any USB webcam at the specified index).
    session.add(
        UVCWebcamStream(
            id="mac_webcam",
            device_index=webcam_index,
            output_dir=output_dir,
            width=width,
            height=height,
            fps=fps,
        )
    )

    # iPhone via Continuity Camera — treated exactly like a UVC webcam.
    session.add(
        UVCWebcamStream(
            id="iphone",
            device_index=iphone_index,
            output_dir=output_dir,
            width=width,
            height=height,
            fps=fps,
        )
    )

    return session


def probe_camera_indices(max_index: int = 5) -> None:
    """Print which OpenCV device indices are currently openable.

    Run with ``--probe`` before your first recording to confirm which
    index is the built-in webcam and which is the iPhone. The iPhone
    only shows up after Continuity Camera is active (wake the phone
    and hold it near the Mac if it doesn't appear).
    """
    try:
        import cv2
    except ImportError:
        print(
            "opencv-python is not installed. Run:\n"
            "  pip install 'syncfield[uvc]'",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Probing OpenCV device indices 0..{max_index - 1} ...\n")
    for idx in range(max_index):
        cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            print(f"  [{idx}] not available")
            continue
        ok, _ = cap.read()
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        status = "OK" if ok else "open but read() failed"
        print(f"  [{idx}] {status}  {width}x{height} @ {fps:.0f} fps")
    print(
        "\nPick the index that matches your Mac webcam and iPhone,"
        " then rerun without --probe."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Record Mac built-in webcam + iPhone (Continuity) through"
            " the SyncField SDK, with the desktop viewer."
        ),
    )
    parser.add_argument(
        "--webcam-index",
        type=int,
        default=0,
        help="OpenCV device index for the Mac built-in webcam (default: 0).",
    )
    parser.add_argument(
        "--iphone-index",
        type=int,
        default=1,
        help=(
            "OpenCV device index for the iPhone Continuity Camera"
            " (default: 1)."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./output"),
        help="Directory where video + session artifacts are written.",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=1920,
        help="Requested frame width in pixels (default: 1920).",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=1080,
        help="Requested frame height in pixels (default: 1080).",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=30.0,
        help="Requested frame rate in Hz (default: 30).",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help=(
            "Don't record — just probe which OpenCV device indices"
            " are currently openable and print their geometry."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.probe:
        probe_camera_indices()
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)

    session = build_session(
        output_dir=args.output_dir,
        webcam_index=args.webcam_index,
        iphone_index=args.iphone_index,
        width=args.width,
        height=args.height,
        fps=args.fps,
    )

    print(f"Session built. Output directory: {args.output_dir.resolve()}")
    print(
        "Opening the SyncField desktop viewer. Click Record to start,"
        " Stop when done. Close the window to finalize."
    )

    # Blocking viewer launch. The viewer runs its own event loop and
    # drives session.start() / session.stop() on worker threads when
    # the user clicks the Record / Stop buttons. `launch()` returns
    # when the user closes the viewer window.
    try:
        import syncfield.viewer
    except ImportError:
        print(
            "\nsyncfield.viewer is not installed. Run:\n"
            "  pip install 'syncfield[viewer]'\n",
            file=sys.stderr,
        )
        return 1

    syncfield.viewer.launch(session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
