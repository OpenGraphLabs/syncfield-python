import type { EpisodeSummary } from "@/lib/review-types";
import { cn } from "@/lib/utils";

interface EpisodeCardProps {
  episode: EpisodeSummary;
  onClick: () => void;
}

export function EpisodeCard({ episode, onClick }: EpisodeCardProps) {
  const date = formatDate(episode.created_at);

  return (
    <button
      onClick={onClick}
      className={cn(
        "w-full overflow-hidden rounded-xl border bg-card text-left transition-all",
        "hover:border-foreground/20 hover:shadow-sm",
      )}
    >
      {/* Thumbnail area */}
      <div className="flex aspect-video items-center justify-center bg-black/90 p-4">
        <div className="flex gap-1.5">
          {Array.from({ length: Math.min(episode.stream_count, 4) }).map(
            (_, i) => (
              <div
                key={i}
                className="aspect-video w-16 rounded-sm bg-white/10"
              />
            ),
          )}
        </div>
      </div>

      {/* Info */}
      <div className="p-3">
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
