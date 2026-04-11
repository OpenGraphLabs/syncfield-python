"""Record Mac webcam + iPhone (Continuity Camera) through SyncField.

    pip install "syncfield[uvc,viewer]"
    python record.py
"""

import argparse
import secrets
from datetime import datetime
from pathlib import Path

import syncfield as sf
import syncfield.viewer
from syncfield.adapters import UVCWebcamStream

OUTPUT_ROOT = Path(__file__).parent / "output"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--webcam-index", type=int, default=0)
    parser.add_argument("--iphone-index", type=int, default=1)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_ROOT)
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    episode = args.output_dir / f"ep_{stamp}_{secrets.token_hex(3)}"
    episode.mkdir(parents=True, exist_ok=True)

    session = sf.SessionOrchestrator(
        host_id="mac_studio",
        output_dir=episode,
    )
    session.add(UVCWebcamStream("mac_webcam", args.webcam_index, episode))
    session.add(UVCWebcamStream("iphone",     args.iphone_index, episode))

    syncfield.viewer.launch(session)


if __name__ == "__main__":
    main()
