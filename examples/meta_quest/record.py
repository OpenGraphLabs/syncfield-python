"""Record Quest 3 head + hands + stereo camera + 4 BLE IMUs.

pip install "syncfield[ble,viewer,camera]"
python record.py
"""

from pathlib import Path

import syncfield as sf
import syncfield.viewer
from syncfield.adapters import (
    MetaQuestHandStream,
    # BLEImuGenericStream,
    # MetaQuestCameraStream,
    UVCWebcamStream,
)

# from syncfield.adapters.ble_imu_profiles import WIT_WT901BLE_200HZ
from syncfield.adapters.meta_quest import discover_quest_ip

quest_ip = discover_quest_ip() or exit(
    "Quest not found — is the SyncField Quest Sender app running?"
)

session = sf.SessionOrchestrator(
    host_id="mac",
    output_dir=Path(__file__).parent / "output",
)

session.add(UVCWebcamStream("iphone", device_index=1, output_dir=session.output_dir))
session.add(MetaQuestHandStream("quest_tracking", quest_host=quest_ip))
# session.add(MetaQuestCameraStream("quest_cam", quest_host=quest_ip, output_dir=session.output_dir))
# session.add(BLEImuGenericStream("wrist_left_imu",  profile=WIT_WT901BLE_200HZ, address="5622CCC4-A621-96DC-A7B5-E7650370E8A3"))
# session.add(BLEImuGenericStream("wrist_right_imu", profile=WIT_WT901BLE_200HZ, address="6E22ED0E-72CD-0175-6F29-0BA8D502CBAB"))
# session.add(BLEImuGenericStream("elbow_left_imu",  profile=WIT_WT901BLE_200HZ, address="1CD2DCDE-CE20-905E-7D66-66E20FB01AB6"))
# session.add(BLEImuGenericStream("elbow_right_imu", profile=WIT_WT901BLE_200HZ, address="C7CA16B4-AFF6-CC54-C657-83836E96979A"))

syncfield.viewer.launch(session)
