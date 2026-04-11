"""Record Mac webcam + iPhone (Continuity Camera) through SyncField.

    pip install "syncfield[uvc,viewer]"
    python record.py
"""

from pathlib import Path

import syncfield as sf
import syncfield.viewer
from syncfield.adapters import UVCWebcamStream

session = sf.SessionOrchestrator(
    host_id="mac_studio",
    output_dir=Path(__file__).parent / "output",
)
session.add(UVCWebcamStream("mac_webcam", device_index=0, output_dir=session.output_dir))
session.add(UVCWebcamStream("iphone",     device_index=1, output_dir=session.output_dir))

syncfield.viewer.launch(session)
