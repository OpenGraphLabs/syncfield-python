"""Record Mac webcam + iPhone + OAK-D-Lite + OAK-D-S2 + OGLO glove.

    pip install "syncfield[uvc,oak,ble,audio,viewer]"
    python record.py
"""

import argparse
from pathlib import Path

import syncfield as sf
import syncfield.viewer
from syncfield.adapters import (
    OakCameraStream,
    OgloTactileStream,
    UVCWebcamStream,
)

DEFAULT_OAK_LITE_SERIAL = "19443010813AF02C00"
DEFAULT_OAK_D_SERIAL    = "1944301071781C1300"
DEFAULT_OGLO_ADDRESS    = "C1718989-5A77-F3EB-B00A-01A758D99D54"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--webcam-index", type=int, default=0)
    parser.add_argument("--iphone-index", type=int, default=1)
    parser.add_argument("--oak-lite", default=DEFAULT_OAK_LITE_SERIAL)
    parser.add_argument("--oak-d",    default=DEFAULT_OAK_D_SERIAL)
    parser.add_argument("--oglo-address", default=DEFAULT_OGLO_ADDRESS)
    parser.add_argument("--oglo-hand", default="right", choices=("left", "right"))
    parser.add_argument("--output-dir", type=Path, default=Path("./output"))
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    session = sf.SessionOrchestrator(
        host_id="mac_studio",
        output_dir=args.output_dir,
    )
    session.add(UVCWebcamStream("mac_webcam", args.webcam_index, args.output_dir))
    session.add(UVCWebcamStream("iphone",     args.iphone_index, args.output_dir))
    session.add(OakCameraStream("oak_lite", args.output_dir, device_id=args.oak_lite))
    session.add(OakCameraStream("oak_d",    args.output_dir, device_id=args.oak_d))
    session.add(OgloTactileStream("oglo", address=args.oglo_address, hand=args.oglo_hand))

    syncfield.viewer.launch(session)


if __name__ == "__main__":
    main()
