import type { EpisodeSummary } from "@/lib/review-types";
import type { AggregationSnapshotWS } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AggregationBadge } from "@/components/aggregation-status-bar";

interface EpisodeCardProps {
  episode: EpisodeSummary;
  onClick: () => void;
  aggregation?: AggregationSnapshotWS;
}

/** Derive aggregation state + percent for this episode. */
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
  const recent = aggregation.recent_jobs.find((j) => j.episode_id === episodeId);
  if (recent) return { state: recent.state, percent: undefined };
  return { state: undefined, percent: undefined };
}

export function EpisodeCard({ episode, onClick, aggregation }: EpisodeCardProps) {
  const date = formatDate(episode.created_at);
  const { state: aggState, percent: aggPct } = getAggState(episode.id, aggregation);

  return (
    <button
      onClick={onClick}
      className={cn(
        "w-full overflow-hidden rounded-lg border bg-card text-left transition-all",
        "hover:border-foreground/20 hover:shadow-sm",
      )}
    >
      {/* Thumbnail area */}
      <div className="flex h-20 items-center justify-center bg-black/90 p-3">
        <div className="flex gap-1">
          {Array.from({ length: Math.min(episode.stream_count, 4) }).map(
            (_, i) => (
              <div
                key={i}
                className="aspect-video w-10 rounded-sm bg-white/10"
              />
            ),
          )}
        </div>
      </div>

      {/* Info */}
      <div className="p-2.5">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate font-mono text-xs font-medium">
              {episode.id}
            </p>
            <p className="mt-0.5 text-[11px] text-muted">{date}</p>
          </div>
        </div>
        <div className="mt-2 flex items-center gap-2">
          {episode.has_sync ? (
            <span className="rounded-md bg-success/10 px-2 py-0.5 text-[10px] font-medium text-success">
              Synced
            </span>
          ) : (
            <span className="rounded-md bg-foreground/5 px-2 py-0.5 text-[10px] font-medium text-muted">
              Not synced
            </span>
          )}
          <span className="text-[10px] text-muted">
            {episode.stream_count} stream
            {episode.stream_count !== 1 ? "s" : ""}
          </span>
          {episode.host_id && (
            <span className="font-mono text-[10px] text-muted">
              {episode.host_id}
            </span>
          )}
          {aggregation && aggState && (
            <AggregationBadge state={aggState} percent={aggPct} />
          )}
        </div>
      </div>
    </button>
  );
}

function formatDate(iso: string): string {
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
