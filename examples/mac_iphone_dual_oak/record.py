"""Record Mac webcam + iPhone + OAK-D-Lite + OAK-D-S2 through SyncField.

    pip install "syncfield[uvc,oak,audio,viewer]"
    python record.py
"""

from pathlib import Path

import syncfield as sf
import syncfield.viewer
from syncfield.adapters import OakCameraStream, UVCWebcamStream

session = sf.SessionOrchestrator(
    host_id="mac_studio",
    output_dir=Path(__file__).parent / "output",
)
out = session.output_dir

session.add(UVCWebcamStream("mac_webcam", device_index=0, output_dir=out))
session.add(UVCWebcamStream("iphone",     device_index=1, output_dir=out))
session.add(OakCameraStream("oak_lite", out, device_id="19443010813AF02C00"))
session.add(OakCameraStream("oak_d",    out, device_id="1944301071781C1300"))

syncfield.viewer.launch(session)
