# Multi-Host Lab Example

Two MacBooks, one research-lab session, zero manual coordination.

## Install

On **every** host, same LAN:

```bash
pip install "syncfield[multihost,uvc,audio,viewer]"
```

## Run

```bash
# Leader MacBook (operator sits here — viewer opens in browser)
python examples/multihost_lab/leader.py

# Follower MacBook (headless — change host_id inside follower.py per host)
python examples/multihost_lab/follower.py
```

## UI — everything happens in the leader's viewer

The leader script launches `syncfield.viewer`. In your browser:

1. **Cluster panel (right sidebar)** auto-populates as followers come online via mDNS — shows each peer's host_id, role, live fps/dropped/disk, RTT.
2. **Record button** — starts the whole cluster atomically: leader plays rising chirp, followers auto-attach, every host begins recording.
3. **Stop button** — falling chirp, every host stops.
4. **Collect Data button** (leader-only, appears in the cluster panel after stop) — pulls every follower's files into a flat `./output/<session_id>/<leader_ep>/` tree with `<host>.<filename>` naming.

Followers have no UI — they just block until they see the leader, mirror its start/stop, and keep their control plane alive for ~10 min so the leader can pull files.

## What you get

```
examples/multihost_lab/output/
└── lab_session/
    ├── aggregated_manifest.json
    └── ep_20260413_143022_abc123/           ← leader's episode (canonical)
        ├── mac_a.mac_webcam.mp4
        ├── mac_a.iphone.mp4
        ├── mac_a.host_audio.wav             ← captured leader's own chirp
        ├── mac_a.sync_point.json
        ├── mac_a.manifest.json
        ├── mac_b.mac_webcam.mp4             ← pulled from follower mac_b
        ├── mac_b.iphone.mp4
        ├── mac_b.host_audio.wav             ← captured leader's chirp through air
        ├── mac_b.sync_point.json
        └── mac_b.manifest.json
```

Each host's `sync_point.json` anchors that host's monotonic clock; each `host_audio.wav` contains the leader's chirp. The downstream sync service uses both for sub-5ms inter-host alignment.

## Requirements

- Same local network (mDNS doesn't traverse routers)
- IPv4 multicast enabled (most home WiFi works; some corporate guest networks isolate clients — try a phone hotspot if mDNS silent-fails)
- Each host: ≥1 audio device (SDK auto-injects a mic stream)
- Leader: working speaker (for the chirp)

## Troubleshooting

- **Follower prints "waiting for leader…" forever** — follower can't see the leader's mDNS advertisement. Check same-WiFi, try `dns-sd -B _syncfield._tcp. local.` on each host.
- **Collect returns empty hosts** — follower's control plane timed out (`keep_alive_after_stop_sec=600s` by default; should be plenty).
- **Single-machine testing** — use `scripts/multihost_local_cluster/` instead; macOS can't resolve mDNS TXT records on loopback so real multi-machine flow needs 2+ hosts.
