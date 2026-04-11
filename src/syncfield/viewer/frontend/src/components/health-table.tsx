import type { HealthEntry } from "@/lib/types";
import { cn } from "@/lib/utils";

interface HealthTableProps {
  entries: HealthEntry[];
}

const KIND_COLORS: Record<string, string> = {
  error: "text-destructive",
  warning: "text-warning",
  drop: "text-destructive",
  reconnect: "text-success",
  heartbeat: "text-muted",
};

/**
 * Compact health event list for the sidebar — newest-first.
 */
export function HealthTable({ entries }: HealthTableProps) {
  if (entries.length === 0) {
    return (
      <div className="px-3 py-8 text-center text-xs text-muted-foreground">
        No health events
      </div>
    );
  }

  const sorted = [...entries].reverse();

  return (
    <ul className="divide-y">
      {sorted.map((entry, i) => (
        <li key={i} className="px-3 py-2">
          <div className="flex items-baseline justify-between gap-2">
            <span
              className={cn(
                "text-[11px] font-medium",
                KIND_COLORS[entry.kind] ?? "text-muted",
              )}
            >
              {entry.kind}
            </span>
            <span className="shrink-0 font-mono text-[10px] text-muted">
              {entry.at_s.toFixed(1)}s
            </span>
          </div>
          <div className="mt-0.5 font-mono text-[11px] text-muted">
            {entry.stream_id}
          </div>
          {entry.detail && (
            <div className="mt-0.5 text-[10px] text-muted-foreground">
              {entry.detail}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}
