# Multi-Host Lab Example

Two MacBooks, one research-lab session, no manual coordination.

## Setup

Install syncfield with the `multihost` extra on **every** host:

```bash
pip install "syncfield[multihost,uvc,audio]"
```

Every host must be on the same local network (mDNS does not traverse routers).

## Run

**Leader MacBook** (`mac_a`):
```bash
python examples/multihost_lab/leader.py
```

**Follower MacBook(s)** (`mac_b`, `mac_c`, …):
```bash
python examples/multihost_lab/follower.py
```

The follower blocks until it sees the leader's mDNS advertisement.
Once the leader calls `session.start()`, the follower proceeds
automatically. Chirp-anchored sync and session config distribution
happen under the hood.

## What happens

1. Each host's orchestrator spins up a local HTTP control plane on
   port 7878 (or OS-assigned on collision) and advertises itself
   via mDNS.
2. Leader's `start()` plays a rising audio chirp. Every host's
   microphone records it — that's the inter-host sync anchor.
3. Leader pushes the session config (session name, chirp spec) to
   every follower over HTTP. Followers validate and apply.
4. Recording runs until the leader calls `stop()` (falling chirp).
5. Each host has its local output under
   `./data/<session_id>/<host_id>/ep_*`.
6. After stop, the leader calls `session.collect_from_followers()`
   to pull each follower's files into one canonical tree at
   `./data/<session_id>/`.

## What you need

- Each host: ≥1 audio-capable stream (microphone). Enforced at `start()`.
- Leader only: working speaker (to emit the chirp).
- All hosts: the same local network, IPv4 multicast enabled (default
  home WiFi is fine; some corporate guest networks aren't — see the
  Multi-Host Sessions doc for the AP isolation workaround).
