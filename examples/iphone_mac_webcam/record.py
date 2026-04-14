"""Record Mac webcam + iPhone + wrist IMU + Insta360 Go3S through SyncField.

    pip install "syncfield[uvc,ble,viewer,camera]"
    python record.py

The Go3S is BLE-triggered (start/stop only). The video file is pulled from the
camera's WiFi AP automatically after stop_recording() — aggregation runs in a
background queue and does not block subsequent recordings.
"""

from pathlib import Path

import syncfield as sf
import syncfield.viewer
from syncfield.adapters import BLEImuGenericStream, Go3SStream, UVCWebcamStream
from syncfield.adapters.ble_imu_profiles import WIT_WT901BLE_200HZ

# Resolved by BleakScanner on 2026-04-14; replace with the address of YOUR Go3S.
# Discover nearby cameras with:
#     python -c "import asyncio; from bleak import BleakScanner; \
#         print(asyncio.run(BleakScanner.discover(timeout=8)))"
GO3S_ADDRESS = "6382B8BC-7438-78A6-E796-EF8DF042ADEE"  # GO 3S 1TEBJJ

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
session.add(Go3SStream(
    "go3s_overhead",
    ble_address=GO3S_ADDRESS,
    output_dir=session.output_dir,
))

syncfield.viewer.launch(session)
