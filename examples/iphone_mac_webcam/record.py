"""Record Mac webcam + iPhone (Continuity Camera) through SyncField.

    pip install "syncfield[uvc,viewer]"
    python record.py
"""

import argparse
from pathlib import Path

import syncfield as sf
import syncfield.viewer
from syncfield.adapters import UVCWebcamStream


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--webcam-index", type=int, default=0)
    parser.add_argument("--iphone-index", type=int, default=1)
    parser.add_argument("--output-dir", type=Path, default=Path("./output"))
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    session = sf.SessionOrchestrator(
        host_id="mac_studio",
        output_dir=args.output_dir,
    )
    session.add(UVCWebcamStream("mac_webcam", args.webcam_index, args.output_dir))
    session.add(UVCWebcamStream("iphone",     args.iphone_index, args.output_dir))

    syncfield.viewer.launch(session)


if __name__ == "__main__":
    main()
