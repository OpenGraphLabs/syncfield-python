import { useState } from "react";
import { useEpisodes } from "@/hooks/use-episodes";
import { cn } from "@/lib/utils";
import { EpisodeCard } from "./episode-card";
import { EpisodeTable } from "./episode-table";
import type { AggregationSnapshotWS } from "@/lib/types";

type ListViewMode = "grid" | "table";

interface EpisodeListProps {
  onSelect: (episodeId: string) => void;
  aggregation?: AggregationSnapshotWS;
}

export function EpisodeList({ onSelect, aggregation }: EpisodeListProps) {
  const { episodes, isLoading, error, refresh } = useEpisodes();
  const [viewMode, setViewMode] = useState<ListViewMode>("table");

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted">
        Loading episodes…
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2">
        <p className="text-sm text-destructive">{error}</p>
        <button
          onClick={refresh}
          className="rounded-lg border px-3 py-1 text-xs font-medium hover:bg-foreground/5"
        >
          Retry
        </button>
      </div>
    );
  }

  if (episodes.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted">
        No episodes found
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar */}
      <div className="flex items-center justify-between border-b px-4 py-2">
        <span className="text-xs text-muted">
          {episodes.length} episode{episodes.length !== 1 ? "s" : ""}
        </span>
        <div className="flex items-center gap-1">
          <button
            onClick={refresh}
            className="rounded-md p-1.5 text-muted transition-colors hover:bg-foreground/5 hover:text-foreground"
            title="Refresh"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
              <path
                d="M2.5 8a5.5 5.5 0 0 1 9.3-4M13.5 8a5.5 5.5 0 0 1-9.3 4"
                stroke="currentColor"
                strokeWidth="1.3"
                strokeLinecap="round"
              />
              <path
                d="M12 2.5v2h-2M4 11.5v2h2"
                stroke="currentColor"
                strokeWidth="1.3"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
          <ViewToggle mode={viewMode} onChange={setViewMode} />
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4">
        {viewMode === "grid" ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
            {episodes.map((ep) => (
              <EpisodeCard
                key={ep.id}
                episode={ep}
                onClick={() => onSelect(ep.id)}
                aggregation={aggregation}
              />
            ))}
          </div>
        ) : (
          <EpisodeTable
            episodes={episodes}
            onSelect={onSelect}
            aggregation={aggregation}
          />
        )}
      </div>
    </div>
  );
}

function ViewToggle({
  mode,
  onChange,
}: {
  mode: ListViewMode;
  onChange: (m: ListViewMode) => void;
}) {
  return (
    <div className="flex rounded-md border">
      <button
        onClick={() => onChange("grid")}
        className={cn(
          "p-1.5 transition-colors",
          mode === "grid"
            ? "bg-foreground/5 text-foreground"
            : "text-muted hover:text-foreground",
        )}
        title="Grid view"
      >
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
          <rect x="2" y="2" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.2" />
          <rect x="9" y="2" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.2" />
          <rect x="2" y="9" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.2" />
          <rect x="9" y="9" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.2" />
        </svg>
      </button>
      <button
        onClick={() => onChange("table")}
        className={cn(
          "border-l p-1.5 transition-colors",
          mode === "table"
            ? "bg-foreground/5 text-foreground"
            : "text-muted hover:text-foreground",
        )}
        title="Table view"
      >
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
          <line x1="2" y1="4" x2="14" y2="4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          <line x1="2" y1="8" x2="14" y2="8" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          <line x1="2" y1="12" x2="14" y2="12" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
        </svg>
      </button>
    </div>
  );
}
