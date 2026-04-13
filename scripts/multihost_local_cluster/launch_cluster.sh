#!/usr/bin/env bash
# Spin up a local 3-host cluster (1 leader + 2 followers) in tmux panes.
# Requires: tmux. Terminals show each process's stdout separately.
#
# Usage: ./scripts/multihost_local_cluster/launch_cluster.sh
# Ctrl-B then D to detach, `tmux attach -t sf_cluster` to re-attach,
# `tmux kill-session -t sf_cluster` to tear down.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SESSION=sf_cluster

cd "$REPO_ROOT"

if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux not found. Install it (brew install tmux) or run the 3 scripts in 3 terminals manually:"
    echo "  python scripts/multihost_local_cluster/follower.py --host-id mac_b --control-plane-port 7879"
    echo "  python scripts/multihost_local_cluster/follower.py --host-id mac_c --control-plane-port 7880"
    echo "  python scripts/multihost_local_cluster/leader.py --host-id mac_a --control-plane-port 7878 --recording-seconds 5"
    exit 1
fi

tmux kill-session -t $SESSION 2>/dev/null || true

tmux new-session -d -s $SESSION -n follower_b \
    "uv run python scripts/multihost_local_cluster/follower.py --host-id mac_b --control-plane-port 7879; read -p 'Enter to close'"

tmux new-window -t $SESSION -n follower_c \
    "uv run python scripts/multihost_local_cluster/follower.py --host-id mac_c --control-plane-port 7880; read -p 'Enter to close'"

# Give followers a moment to start advertising before launching the leader.
sleep 2

tmux new-window -t $SESSION -n leader \
    "uv run python scripts/multihost_local_cluster/leader.py \
        --host-id mac_a --control-plane-port 7878 --recording-seconds 5 \
        --follower mac_b:7879 \
        --follower mac_c:7880; \
        read -p 'Enter to close'"

echo "Launched local cluster in tmux session '$SESSION'."
echo "Attach with:  tmux attach -t $SESSION"
echo "Switch windows with Ctrl-B then 0/1/2."
echo "Kill with:    tmux kill-session -t $SESSION"
