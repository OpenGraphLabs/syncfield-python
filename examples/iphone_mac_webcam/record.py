"""Record Mac webcam + iPhone + wrist IMU + Insta360 Go3S through SyncField.

    pip install "syncfield[uvc,ble,viewer,camera]"
    python record.py

Workflow (wrist-mount + later dock-and-sync):
  1. Wear the Go3S on your wrist. Its WiFi is OFF — only BLE is needed.
  2. Click **Record** in the viewer → all sensors + Go3S start in sync.
  3. Click **Stop** → recording ends, episode dir now contains an
     ``aggregation.json`` with the SD file path for the Go3S clip.
     (No file download yet.)
  4. Optional: record more episodes the same way.
  5. When ready to pull videos off the camera:
     a. Dock the Go3S in the Action Pod (or turn WiFi ON manually via
        the camera: swipe down → WiFi → ON).
     b. Make sure the camera's WiFi AP (e.g. "GO 3S 1TEBJJ.OSC") is
        visible in macOS WiFi menu.
     c. Click **Sync Go3S** in the viewer. The background queue walks
        every pending ``aggregation.json`` under the output directory,
        switches to the camera AP, downloads each file, and restores
        the original WiFi network.

Go3S policy defaults to ``on_demand`` so recording never blocks on WiFi
availability. Pass ``aggregation_policy="eager"`` if you want stop to
auto-download (only useful when camera is docked the whole time).

macOS one-time setup (unavoidable — OS-enforced):
  * Grant Location Services permission to Python / your terminal app when
    prompted — required for programmatic WiFi switching on macOS 13+.
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
