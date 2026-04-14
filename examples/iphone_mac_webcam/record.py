"""Record Mac webcam + iPhone (Continuity Camera) + wrist IMU through SyncField.

    pip install "syncfield[uvc,ble,viewer]"
    python record.py
"""

from pathlib import Path

import syncfield as sf
import syncfield.viewer
from syncfield.adapters import BLEImuGenericStream, UVCWebcamStream
from syncfield.adapters.ble_imu_profiles import WIT_WT901BLE_200HZ

session = sf.SessionOrchestrator(
    host_id="mac_studio",
    output_dir=Path(__file__).parent / "output",
)
session.add(UVCWebcamStream("mac_webcam", device_index=0, output_dir=session.output_dir))
session.add(UVCWebcamStream("iphone",     device_index=1, output_dir=session.output_dir))
session.add(BLEImuGenericStream(
    "wrist_left_imu",
    profile=WIT_WT901BLE_200HZ,
    ble_name="WT901BLE",
))

syncfield.viewer.launch(session)
