# Local multi-host test cluster

Spins up a 1-leader + 2-follower SyncField cluster on a single machine
for smoke-testing the multi-host pipeline without requiring 2 physical
MacBooks.

## What this validates

- ✅ mDNS discovery (loopback works)
- ✅ HTTP control plane on localhost (port 7878/7879/7880)
- ✅ Session config distribution (leader → followers)
- ✅ Follower lifecycle (start → wait-for-leader → mirror stop)
- ✅ Control plane keep-alive window + DELETE preemption
- ✅ `collect_from_followers()` pulling real files over HTTP
- ✅ `aggregated_manifest.json` shape

## What this does NOT validate

- ❌ Real chirp-based inter-host sync accuracy — two processes on one
  machine share the same speaker and mic, so the chirp round-trip is
  meaningless. `sync_tone=SyncToneConfig.silent()` in both scripts.
  To measure actual sync quality, use 2+ real hosts in the same room.

For full sync-quality validation, use 2+ real MacBooks in the same
room with `examples/multihost_lab/`.

## Quick start

```bash
# With tmux (recommended — three-pane view):
./scripts/multihost_local_cluster/launch_cluster.sh
tmux attach -t sf_cluster

# Without tmux (three manual terminals):
uv run python scripts/multihost_local_cluster/follower.py --host-id mac_b --control-plane-port 7879
uv run python scripts/multihost_local_cluster/follower.py --host-id mac_c --control-plane-port 7880
uv run python scripts/multihost_local_cluster/leader.py --host-id mac_a --control-plane-port 7878 --recording-seconds 5
```

## Expected output

Leader:
```
[leader mac_a] starting session local-test-cluster
[leader mac_a] control plane listening on :7878
[leader mac_a] recording for 5.0s…
[leader mac_a] stopping…
[leader mac_a] collecting files from followers (inside 30.0s window)…
[leader mac_a] aggregated report:
  session_id: local-test-cluster
  leader_host_id: mac_a
  mac_b: status=ok files=2
  mac_c: status=ok files=2
[leader mac_a] done.
```

After the run, inspect the output tree:
```bash
tree data_leader/local-test-cluster/
```

You should see:
```
data_leader/local-test-cluster/
├── aggregated_manifest.json
├── mac_b/
│   └── <files from follower b's output>
└── mac_c/
    └── <files from follower c's output>
```

## macOS loopback caveat

On macOS, python-zeroconf cannot resolve TXT records for services
advertised by another process on the same machine (loopback). The
`add_service` callback fires but `get_service_info` returns None,
so the leader's mDNS browser sees the follower's name but no
metadata.

Workaround: the local test cluster bypasses mDNS via static peers.
The launcher script passes `--follower mac_b:7879 --follower mac_c:7880`
to the leader. Real multi-machine LAN deployments use mDNS as
normal — this is a single-machine testing-only workaround.

The same workaround applies in the other direction: followers cannot
resolve the leader's mDNS TXT record on the same machine, so they
also bypass mDNS. The launcher passes `--leader mac_a@127.0.0.1:7878`
to each follower; the follower polls the leader's /health endpoint
to detect state transitions instead of browsing mDNS.

## Troubleshooting

- **Follower times out waiting for leader**: usually means mDNS didn't
  propagate in time. Try increasing `--leader-wait-timeout-sec`.
- **Port 7878 in use**: change leader's `--control-plane-port`, or let
  the followers use `0` (OS-assigned — the default).
- **No files from a follower**: check the follower's output_dir actually
  got an episode directory created. FakeStream doesn't write bytes but
  session artifacts like manifest.json do land.
