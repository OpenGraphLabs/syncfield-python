# SyncField Examples — Quick Start

## 1. 새 PC 에서 한 번만

```bash
git clone https://github.com/OpenGraphLabs/syncfield-python.git
cd syncfield-python
uv sync --all-extras
```

## 2. 예제 실행 (둘 중 아무 방식이나)

**A. Repo 안에서 직접:**
```bash
uv run python examples/<예제>/record.py
```

**B. PyPI 에서 설치 후:**
```bash
pip install "syncfield[all]"
python examples/<예제>/record.py
```

## 3. 예제 카탈로그

| 예제 | 커맨드 | 필요 하드웨어 |
|------|--------|-------------|
| **Mac 웹캠 + iPhone** | `uv run python examples/iphone_mac_webcam/record.py` | Mac + iPhone (Continuity Camera) |
| **4대 카메라 (Mac+iPhone+OAK×2)** | `uv run python examples/mac_iphone_dual_oak/record.py` | 위 + OAK-D-Lite + OAK-D-S2 |
| **Full rig (5 streams)** | `uv run python examples/full_rig/record.py` | 위 + OGLO BLE 촉각 글러브 |
| **센서 폴링 데모** | `uv run python examples/generic_sensor_demo/polling_serial.py` | Serial 센서 |
| **센서 push 데모** | `uv run python examples/generic_sensor_demo/push_async.py` | 없음 (fake) |
| **Multi-host 리더** | `uv run python examples/multihost_lab/leader.py` | Mac + iPhone |
| **Multi-host 팔로워** | `uv run python examples/multihost_lab/follower.py` | Mac + iPhone |

## 4. Multi-host (맥북 2대)

**같은 WiFi, 같은 LAN 에서:**

```bash
# 맥북 A (leader)
uv run python examples/multihost_lab/leader.py
# 녹화 중… Ctrl-C 로 stop → 자동으로 follower 파일 pull

# 맥북 B (follower)
uv run python examples/multihost_lab/follower.py
# 리더 자동 발견 → 동기 녹화 → 리더 stop 감지 → 자기도 stop
```

Follower 여러 대면 각자 `follower.py` 안의 `host_id="mac_b"` 를 `mac_c`, `mac_d` 등으로 수정.

## 5. 결과 위치

- Single-host: `examples/<예제>/output/ep_<timestamp>/`
- Multi-host (leader 에 집약): `examples/multihost_lab/output/<session_id>/<leader_episode>/<host>.<filename>`

## 참고

- 예제별 상세: 각 폴더의 `README.md`
- 카메라/센서 하드웨어 자동 탐색: `uv run python -m syncfield.discovery`
- Audio extra 는 항상 권장 (chirp + countdown 소리용)
