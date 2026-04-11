"""Record Mac webcam + iPhone + OAK-D-Lite + OAK-D-S2 + OGLO glove.

    pip install "syncfield[uvc,oak,ble,audio,viewer]"
    python record.py
"""

from pathlib import Path

import syncfield as sf
import syncfield.viewer
from syncfield.adapters import OakCameraStream, OgloTactileStream, UVCWebcamStream

session = sf.SessionOrchestrator(
    host_id="mac_studio",
    output_dir=Path("./output"),
)
out = session.output_dir

session.add(UVCWebcamStream("mac_webcam", device_index=0, output_dir=out))
session.add(UVCWebcamStream("iphone",     device_index=1, output_dir=out))
session.add(OakCameraStream("oak_lite", out, device_id="19443010813AF02C00"))
session.add(OakCameraStream("oak_d",    out, device_id="1944301071781C1300"))
session.add(OgloTactileStream("oglo", address="C1718989-5A77-F3EB-B00A-01A758D99D54"))

syncfield.viewer.launch(session)
