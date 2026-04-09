"""Record Mac webcam + iPhone + OAK-D-Lite + OAK-D-S2 through SyncField.

    pip install "syncfield[uvc,oak,audio,viewer]"
    python record.py
"""

import argparse
import secrets
from datetime import datetime
from pathlib import Path

import syncfield as sf
import syncfield.viewer
from syncfield.adapters import OakCameraStream, UVCWebcamStream

OUTPUT_ROOT = Path(__file__).parent / "output"

# Override with --oak-lite / --oak-d if your rig differs.
DEFAULT_OAK_LITE_SERIAL = "19443010813AF02C00"
DEFAULT_OAK_D_SERIAL    = "1944301071781C1300"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--webcam-index", type=int, default=0)
    parser.add_argument("--iphone-index", type=int, default=1)
    parser.add_argument("--oak-lite", default=DEFAULT_OAK_LITE_SERIAL)
    parser.add_argument("--oak-d",    default=DEFAULT_OAK_D_SERIAL)
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
    session.add(OakCameraStream("oak_lite", episode, device_id=args.oak_lite))
    session.add(OakCameraStream("oak_d",    episode, device_id=args.oak_d))

    syncfield.viewer.launch(session)


if __name__ == "__main__":
    main()
