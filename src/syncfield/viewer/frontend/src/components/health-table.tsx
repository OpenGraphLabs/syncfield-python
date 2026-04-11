import type { HealthEntry } from "@/lib/types";

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
 * Health event timeline — newest-first table of stream health events.
 */
export function HealthTable({ entries }: HealthTableProps) {
  if (entries.length === 0) {
    return (
      <div className="px-4 py-6 text-center text-xs text-muted">
        No health events
      </div>
    );
  }

  // Display newest first
  const sorted = [...entries].reverse();

  return (
    <div className="overflow-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b text-left text-muted">
            <th className="px-4 py-2 font-medium">Time</th>
            <th className="px-4 py-2 font-medium">Stream</th>
            <th className="px-4 py-2 font-medium">Kind</th>
            <th className="px-4 py-2 font-medium">Detail</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((entry, i) => (
            <tr key={i} className="border-b last:border-0">
              <td className="whitespace-nowrap px-4 py-1.5 font-mono">
                {entry.at_s.toFixed(3)}s
              </td>
              <td className="whitespace-nowrap px-4 py-1.5 font-mono">
                {entry.stream_id}
              </td>
              <td className="whitespace-nowrap px-4 py-1.5">
                <span className={KIND_COLORS[entry.kind] ?? "text-muted"}>
                  {entry.kind}
                </span>
              </td>
              <td className="px-4 py-1.5 text-muted">
                {entry.detail ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
