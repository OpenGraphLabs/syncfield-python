import type { EpisodeSummary } from "@/lib/review-types";
import type { AggregationSnapshotWS } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AggregationBadge } from "@/components/aggregation-status-bar";

interface EpisodeTableProps {
  episodes: EpisodeSummary[];
  onSelect: (id: string) => void;
  aggregation?: AggregationSnapshotWS;
}

/** Derive aggregation state + percent for a given episode from the WS snapshot. */
function getAggState(
  episodeId: string,
  aggregation: AggregationSnapshotWS | undefined,
): { state: string | undefined; percent: number | undefined } {
  if (!aggregation) return { state: undefined, percent: undefined };

  const active = aggregation.active_job;
  if (active && active.episode_id === episodeId) {
    const pct =
      active.current_total_bytes > 0
        ? Math.round((active.current_bytes / active.current_total_bytes) * 100)
        : 0;
    return { state: active.state, percent: pct };
  }

  const recent = aggregation.recent_jobs.find(
    (j) => j.episode_id === episodeId,
  );
  if (recent) return { state: recent.state, percent: undefined };

  return { state: undefined, percent: undefined };
}

export function EpisodeTable({ episodes, onSelect, aggregation }: EpisodeTableProps) {
  return (
    <div className="overflow-auto rounded-xl border bg-card">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b text-left text-muted">
            <th className="px-4 py-2.5 font-medium">Episode</th>
            <th className="px-4 py-2.5 font-medium">Task</th>
            <th className="px-4 py-2.5 font-medium">Date</th>
            <th className="px-4 py-2.5 font-medium">Host</th>
            <th className="px-4 py-2.5 font-medium">Streams</th>
            <th className="px-4 py-2.5 font-medium">Sync</th>
            {aggregation && (
              <th className="px-4 py-2.5 font-medium">Aggregation</th>
            )}
          </tr>
        </thead>
        <tbody>
          {episodes.map((ep) => (
            <tr
              key={ep.id}
              onClick={() => onSelect(ep.id)}
              className={cn(
                "cursor-pointer border-b last:border-0",
                "transition-colors hover:bg-foreground/3",
              )}
            >
              <td className="px-4 py-2.5 font-mono font-medium">{ep.id}</td>
              <td className="px-4 py-2.5 text-muted">{ep.task ?? "—"}</td>
              <td className="px-4 py-2.5 text-muted">
                {formatCompact(ep.created_at)}
              </td>
              <td className="px-4 py-2.5 font-mono text-muted">
                {ep.host_id ?? "—"}
              </td>
              <td className="px-4 py-2.5 text-muted">{ep.stream_count}</td>
              <td className="px-4 py-2.5">
                {ep.has_sync ? (
                  <span className="text-success">Synced</span>
                ) : (
                  <span className="text-muted">—</span>
                )}
              </td>
              {aggregation && (() => {
                const { state, percent } = getAggState(ep.id, aggregation);
                return (
                  <td className="px-4 py-2.5">
                    <AggregationBadge state={state} percent={percent} />
                  </td>
                );
              })()}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatCompact(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
