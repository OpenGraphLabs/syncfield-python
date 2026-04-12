import type { HealthEntry } from "@/lib/types";
import { cn } from "@/lib/utils";

interface HealthTableProps {
  entries: HealthEntry[];
}

const KIND_ICONS: Record<string, string> = {
  heartbeat: "●",
  warning: "⚠",
  error: "✗",
  drop: "↓",
  reconnect: "↻",
};

const KIND_COLORS: Record<string, string> = {
  error: "text-destructive",
  warning: "text-warning",
  drop: "text-destructive",
  reconnect: "text-success",
  heartbeat: "text-success",
};

/**
 * Compact health event timeline for the sidebar.
 */
export function HealthTable({ entries }: HealthTableProps) {
  if (entries.length === 0) {
    return (
      <div className="px-3 py-8 text-center text-xs text-muted-foreground">
        No events yet
      </div>
    );
  }

  const sorted = [...entries].reverse();

  return (
    <ul className="divide-y divide-border/50">
      {sorted.map((entry, i) => (
        <li key={i} className="flex items-start gap-2 px-3 py-2">
          {/* Icon */}
          <span
            className={cn(
              "mt-0.5 shrink-0 text-[10px]",
              KIND_COLORS[entry.kind] ?? "text-muted",
            )}
          >
            {KIND_ICONS[entry.kind] ?? "·"}
          </span>

          {/* Content */}
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline justify-between gap-1">
              <span className="font-mono text-[11px] font-medium">
                {entry.stream_id}
              </span>
              <span className="shrink-0 text-[9px] text-muted">
                {formatAgo(entry.ago_s)}
              </span>
            </div>
            {entry.detail && (
              <div className="text-[10px] text-muted">
                {entry.detail}
              </div>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}

function formatAgo(seconds: number): string {
  if (seconds < 1) return "just now";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}
