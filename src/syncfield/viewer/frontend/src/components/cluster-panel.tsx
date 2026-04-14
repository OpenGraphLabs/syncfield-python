import { useMemo, useState } from "react";
import type {
  ClusterPeer,
  ClusterHostHealth,
  ClusterStreamHealth,
} from "@/lib/types";
import type { UseClusterReturn } from "@/hooks/use-cluster";
import { cn } from "@/lib/utils";
import { ClusterControls } from "./cluster-controls";
import { ClusterConfigBadge } from "./cluster-config-badge";

interface ClusterPanelProps {
  cluster: UseClusterReturn;
  onDiscoverAcrossCluster?: () => void;
}

/**
 * Sidebar panel summarising the multi-host cluster: aggregate status
 * strip, peer list with live per-host stream health, the applied config,
 * and leader-only actions.
 *
 * Renders nothing when `cluster.available === false` (single-host session).
 */
export function ClusterPanel({
  cluster,
  onDiscoverAcrossCluster,
}: ClusterPanelProps) {
  if (!cluster.available) return null;

  const peers = cluster.peers;
  const hosts = cluster.health?.hosts ?? [];
  const healthByHost = new Map<string, ClusterHostHealth>(
    hosts.map((h) => [h.host_id, h]),
  );

  const sessionId = peers?.session_id ?? null;
  const peerList = peers?.peers ?? [];
  const peerCount = peerList.length;

  const summary = useMemo(
    () => computeClusterSummary(peerList, healthByHost),
    [peerList, healthByHost],
  );

  return (
    <div className="flex flex-col gap-3 border-b px-3 py-3">
      {/* Header row */}
      <div className="flex items-center gap-2">
        <span className="text-[10px] text-muted" aria-hidden>
          ⌁
        </span>
        <h3 className="text-xs font-medium">Cluster</h3>
        {sessionId && (
          <span className="truncate rounded-md bg-foreground/5 px-1.5 py-0.5 font-mono text-[10px] text-muted">
            {sessionId}
          </span>
        )}
        <div className="flex-1" />
        <span className="text-[10px] text-muted">
          {peerCount} {peerCount === 1 ? "host" : "hosts"}
        </span>
      </div>

      {/* Aggregate status strip */}
      {peerCount > 0 && <ClusterSummaryStrip summary={summary} />}

      {/* Applied config badge (optional) */}
      <ClusterConfigBadge config={cluster.config} />

      {/* Peer list */}
      <ul className="flex flex-col gap-1">
        {peerList.map((peer) => (
          <PeerRow
            key={peer.host_id}
            peer={peer}
            health={healthByHost.get(peer.host_id)}
          />
        ))}
        {peerCount === 0 && <EmptyPeerState isLeader={cluster.isLeader} />}
        {peerCount === 1 && peerList[0]?.is_self && cluster.isLeader && (
          <li className="rounded-md border border-dashed px-2 py-2 text-[11px] text-muted">
            <div className="flex items-center gap-1.5">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-warning" />
              <span>Waiting for followers to join…</span>
            </div>
            <div className="mt-1 pl-3 text-[10px]">
              Run <code className="rounded bg-foreground/10 px-1">follower.py</code> on another machine on the same LAN.
            </div>
          </li>
        )}
      </ul>

      {/* Action row */}
      {cluster.isLeader && (
        <div className="flex flex-col gap-2 border-t pt-3">
          <ClusterControls cluster={cluster} summary={summary} />
          {onDiscoverAcrossCluster && (
            <button
              onClick={onDiscoverAcrossCluster}
              className={cn(
                "self-start rounded-lg border px-3 py-1 text-xs font-medium",
                "transition-colors hover:bg-foreground/5",
              )}
              title="Ask every follower to list the devices it can see (for cross-cluster device setup)"
            >
              Discover Across Cluster
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cluster summary
// ---------------------------------------------------------------------------

export interface ClusterSummary {
  total: number;
  recording: number;
  preparing: number;
  ready: number;
  stopped: number;
  unreachable: number;
  /** True when every non-self reachable peer is in the same state. */
  allRecording: boolean;
  allStopped: boolean;
}

function computeClusterSummary(
  peers: ClusterPeer[],
  healthByHost: Map<string, ClusterHostHealth>,
): ClusterSummary {
  const summary: ClusterSummary = {
    total: peers.length,
    recording: 0,
    preparing: 0,
    ready: 0,
    stopped: 0,
    unreachable: 0,
    allRecording: false,
    allStopped: false,
  };

  for (const peer of peers) {
    const state = derivePeerState(peer, healthByHost.get(peer.host_id));
    switch (state) {
      case "recording":
        summary.recording += 1;
        break;
      case "preparing":
        summary.preparing += 1;
        break;
      case "ready":
        summary.ready += 1;
        break;
      case "stopped":
        summary.stopped += 1;
        break;
      case "unreachable":
        summary.unreachable += 1;
        break;
    }
  }

  if (summary.total > 0) {
    summary.allRecording = summary.recording === summary.total;
    summary.allStopped = summary.stopped === summary.total;
  }
  return summary;
}

function ClusterSummaryStrip({ summary }: { summary: ClusterSummary }) {
  const chips: Array<{ label: string; count: number; cls: string }> = [];
  if (summary.recording > 0) {
    chips.push({
      label: "recording",
      count: summary.recording,
      cls: "bg-recording/15 text-recording",
    });
  }
  if (summary.preparing > 0) {
    chips.push({
      label: "preparing",
      count: summary.preparing,
      cls: "bg-warning/15 text-warning",
    });
  }
  if (summary.ready > 0) {
    chips.push({
      label: "ready",
      count: summary.ready,
      cls: "bg-foreground/5 text-muted",
    });
  }
  if (summary.stopped > 0) {
    chips.push({
      label: "stopped",
      count: summary.stopped,
      cls: "bg-foreground/5 text-muted",
    });
  }
  if (summary.unreachable > 0) {
    chips.push({
      label: "unreachable",
      count: summary.unreachable,
      cls: "bg-destructive/15 text-destructive",
    });
  }

  if (chips.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1">
      {chips.map((c) => (
        <span
          key={c.label}
          className={cn(
            "rounded-full px-2 py-0.5 text-[10px] font-medium tabular-nums",
            c.cls,
          )}
        >
          {c.count} {c.label}
        </span>
      ))}
    </div>
  );
}

function EmptyPeerState({ isLeader }: { isLeader: boolean }) {
  return (
    <li className="rounded-md border border-dashed px-2 py-3 text-center text-[11px] text-muted">
      {isLeader ? (
        <>
          <div>No peers discovered yet</div>
          <div className="mt-1 text-[10px]">
            Followers on the same LAN will appear here automatically.
          </div>
        </>
      ) : (
        <div>Not attached to a leader</div>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// PeerRow
// ---------------------------------------------------------------------------

type DerivedState =
  | "recording"
  | "preparing"
  | "ready"
  | "stopped"
  | "idle"
  | "unreachable";

const STATE_DOT: Record<DerivedState, string> = {
  recording: "bg-recording animate-pulse-recording",
  preparing: "bg-warning animate-pulse",
  ready: "bg-success",
  stopped: "bg-muted",
  idle: "bg-muted",
  unreachable: "bg-destructive",
};

const STATE_LABEL: Record<DerivedState, string> = {
  recording: "recording",
  preparing: "preparing",
  ready: "ready",
  stopped: "stopped",
  idle: "idle",
  unreachable: "unreachable",
};

/**
 * Derive a UI-friendly state by combining the mDNS advert status (peer.status)
 * with the orchestrator-reported state (health.health.state from /health).
 * The /health endpoint is authoritative — mDNS TXT can be stale on WiFi.
 */
export function derivePeerState(
  peer: ClusterPeer,
  health?: ClusterHostHealth,
): DerivedState {
  // Self is always reachable regardless of HTTP probe.
  if (!peer.is_self) {
    if (health?.status === "unreachable" || health?.status === "error") {
      return "unreachable";
    }
  }

  // Prefer the authoritative orchestrator state from /health.
  const orchState = health?.health?.state;
  if (orchState) {
    switch (orchState) {
      case "recording":
        return "recording";
      case "preparing":
      case "starting":
        return "preparing";
      case "connected":
        return "ready";
      case "stopped":
      case "stopping":
        return "stopped";
      case "idle":
      case "disconnecting":
      case "connecting":
        return "idle";
      default:
        break;
    }
  }

  // Fall back to the mDNS advert.
  const advert = peer.status;
  if (advert === "recording") return "recording";
  if (advert === "preparing") return "preparing";
  if (advert === "stopped") return "stopped";
  return "idle";
}

function PeerRow({
  peer,
  health,
}: {
  peer: ClusterPeer;
  health?: ClusterHostHealth;
}) {
  const [expanded, setExpanded] = useState(false);
  const state = derivePeerState(peer, health);
  const streams = health?.streams ?? [];
  const hasStreams = streams.length > 0;
  const rtt = health?.rtt_ms;
  const addr = peer.resolved_address;
  const port = peer.control_plane_port;

  return (
    <li
      className={cn(
        "rounded-md border",
        state === "recording" && "border-recording/40",
        state === "unreachable" && "border-destructive/40",
      )}
    >
      <button
        type="button"
        onClick={() => hasStreams && setExpanded((v) => !v)}
        className={cn(
          "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left",
          hasStreams && "hover:bg-foreground/5",
        )}
        aria-expanded={expanded}
        disabled={!hasStreams}
        title={
          addr && port
            ? `${addr}:${port}`
            : "control plane address not resolved yet"
        }
      >
        {/* Role badge */}
        <span
          className={cn(
            "rounded px-1.5 py-0.5 text-[10px] font-medium",
            peer.role === "leader"
              ? "bg-primary/15 text-primary"
              : "bg-foreground/5 text-muted",
          )}
        >
          {peer.role}
        </span>

        {/* host_id */}
        <span className="min-w-0 flex-1 truncate font-mono text-[11px]">
          {peer.host_id}
          {peer.is_self && <span className="ml-1 text-muted">(self)</span>}
        </span>

        {/* State pill */}
        <span className="flex items-center gap-1">
          <span
            className={cn(
              "inline-block h-1.5 w-1.5 rounded-full",
              STATE_DOT[state],
            )}
          />
          <span
            className={cn(
              "text-[10px]",
              state === "recording"
                ? "text-recording"
                : state === "unreachable"
                  ? "text-destructive"
                  : state === "ready"
                    ? "text-success"
                    : state === "preparing"
                      ? "text-warning"
                      : "text-muted",
            )}
          >
            {STATE_LABEL[state]}
          </span>
        </span>

        {/* RTT */}
        {rtt != null && (
          <span
            className="font-mono text-[10px] text-muted tabular-nums"
            title="round-trip time to the host's control plane"
          >
            {rtt.toFixed(0)}ms
          </span>
        )}

        {/* Stream count */}
        {hasStreams && (
          <span className="text-[10px] text-muted">
            {streams.length} stream{streams.length !== 1 ? "s" : ""}
          </span>
        )}

        {/* Chevron */}
        {hasStreams && (
          <span
            className={cn(
              "text-[10px] text-muted transition-transform",
              expanded && "rotate-90",
            )}
            aria-hidden
          >
            ▸
          </span>
        )}
      </button>

      {/* Expanded stream detail */}
      {expanded && hasStreams && (
        <ul className="divide-y divide-border/50 border-t">
          {streams.map((s) => (
            <StreamRow key={s.id} stream={s} />
          ))}
        </ul>
      )}

      {/* Unreachable error tail */}
      {health?.error && (
        <div className="border-t px-2 py-1 text-[10px] text-destructive">
          {health.error}
        </div>
      )}
    </li>
  );
}

function StreamRow({ stream }: { stream: ClusterStreamHealth }) {
  return (
    <li className="flex items-center gap-2 px-3 py-1 text-[10px]">
      <span className="font-mono text-foreground">{stream.id}</span>
      <span className="text-muted">{stream.kind}</span>
      <div className="flex-1" />
      <span className="font-mono tabular-nums text-muted">
        {stream.fps.toFixed(1)} fps
      </span>
      <span className="h-3 w-px bg-border" />
      <span className="font-mono tabular-nums text-muted">
        {stream.frames} frames
      </span>
      {stream.dropped > 0 && (
        <>
          <span className="h-3 w-px bg-border" />
          <span className="text-destructive">{stream.dropped} dropped</span>
        </>
      )}
    </li>
  );
}
