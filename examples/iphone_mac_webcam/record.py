"""Record Mac webcam + iPhone + wrist IMU + Insta360 Go3S through SyncField.

    pip install "syncfield[uvc,ble,viewer,camera]"
    python record.py

Workflow (wrist-mount during recording, USB-C for collection):
  1. Wear the Go3S on your wrist. Only BLE is used — no WiFi.
  2. In the viewer, click Connect → Record → ... → Stop.
  3. After recording, episode dirs hold ``aggregation.json`` with the
     camera's SD path for each Go3S clip. No video copied yet.
  4. When ready to pull videos:
     a. Connect the Go3S to your Mac via USB-C cable.
     b. On the camera screen, when prompted, choose
        "USB / Mass Storage". The SD card mounts as a Finder disk
        (e.g. "Insta360GO3S").
     c. Switch to the Review tab in the viewer and click
        Collect Videos. Files for every pending episode are copied
        from the SD card into their episode folders.

Why USB instead of WiFi:
  Insta360's iOS app can join the camera's WiFi AP via
  ``NEHotspotConfiguration`` + their proprietary BLE wake command (in
  ``INSCameraServiceSDK``). macOS has neither — ``networksetup``
  associations are rejected with -3925 kCWAssociationDeniedErr, and
  the BLE wake command is not in any public reverse-engineered
  protocol. USB Mass Storage sidesteps the whole stack: the SD card
  is just a disk and copying a file is a file system operation.
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
# Both WT901BLE units advertise the same name ("WT901BLE68"), so they must be
# distinguished by address. Resolved via active-scan on 2026-04-14.
session.add(BLEImuGenericStream(
    "wrist_left_imu",
    profile=WIT_WT901BLE_200HZ,
    address="5622CCC4-A621-96DC-A7B5-E7650370E8A3",
))
session.add(BLEImuGenericStream(
    "wrist_right_imu",
    profile=WIT_WT901BLE_200HZ,
    address="6E22ED0E-72CD-0175-6F29-0BA8D502CBAB",
))
session.add(Go3SStream(
    "go3s_overhead",
    ble_address=GO3S_ADDRESS,
    output_dir=session.output_dir,
))

syncfield.viewer.launch(session)
