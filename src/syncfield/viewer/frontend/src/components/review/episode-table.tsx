import type { EpisodeSummary } from "@/lib/review-types";
import { cn } from "@/lib/utils";

interface EpisodeTableProps {
  episodes: EpisodeSummary[];
  onSelect: (id: string) => void;
}

export function EpisodeTable({ episodes, onSelect }: EpisodeTableProps) {
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
