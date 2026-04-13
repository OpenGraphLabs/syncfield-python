import { useState } from "react";
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
 * Sidebar panel summarising the multi-host cluster: peer list, live per-host
 * stream health, the applied config, and leader-only actions.
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
  const peerCount = peers?.peers.length ?? 0;

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
          {peerCount} {peerCount === 1 ? "peer" : "peers"}
        </span>
      </div>

      {/* Applied config badge (optional) */}
      <ClusterConfigBadge config={cluster.config} />

      {/* Peer list */}
      <ul className="flex flex-col gap-1">
        {(peers?.peers ?? []).map((peer) => (
          <PeerRow
            key={peer.host_id}
            peer={peer}
            health={healthByHost.get(peer.host_id)}
          />
        ))}
        {peerCount === 0 && (
          <li className="py-2 text-center text-[11px] text-muted">
            No peers discovered yet
          </li>
        )}
      </ul>

      {/* Action row */}
      {cluster.isLeader && (
        <div className="flex flex-col gap-2 border-t pt-3">
          <ClusterControls cluster={cluster} />
          {onDiscoverAcrossCluster && (
            <button
              onClick={onDiscoverAcrossCluster}
              className={cn(
                "self-start rounded-lg border px-3 py-1 text-xs font-medium",
                "transition-colors hover:bg-foreground/5",
              )}
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
// PeerRow
// ---------------------------------------------------------------------------

const STATE_DOT: Record<string, string> = {
  recording: "bg-recording animate-pulse-recording",
  preparing: "bg-warning",
  stopped: "bg-muted",
  unreachable: "bg-destructive",
};

function effectiveState(peer: ClusterPeer, health?: ClusterHostHealth): string {
  if (health?.status === "unreachable" || health?.status === "error") {
    return "unreachable";
  }
  return peer.status || "stopped";
}

function PeerRow({
  peer,
  health,
}: {
  peer: ClusterPeer;
  health?: ClusterHostHealth;
}) {
  const [expanded, setExpanded] = useState(false);
  const state = effectiveState(peer, health);
  const streams = health?.streams ?? [];
  const hasStreams = streams.length > 0;
  const rtt = health?.rtt_ms;

  return (
    <li className="rounded-md border">
      <button
        type="button"
        onClick={() => hasStreams && setExpanded((v) => !v)}
        className={cn(
          "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left",
          hasStreams && "hover:bg-foreground/5",
        )}
        aria-expanded={expanded}
        disabled={!hasStreams}
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
              STATE_DOT[state] ?? "bg-muted",
            )}
          />
          <span
            className={cn(
              "text-[10px]",
              state === "recording"
                ? "text-recording"
                : state === "unreachable"
                  ? "text-destructive"
                  : "text-muted",
            )}
          >
            {state}
          </span>
        </span>

        {/* RTT */}
        {rtt != null && (
          <span className="font-mono text-[10px] text-muted tabular-nums">
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
