import type { ClusterConfigResponse } from "@/lib/types";

interface ClusterConfigBadgeProps {
  config: ClusterConfigResponse | null;
}

/**
 * Tight one-line summary of the applied session config. Hides when the
 * leader hasn't applied a config yet (fresh session / idle cluster).
 */
export function ClusterConfigBadge({ config }: ClusterConfigBadgeProps) {
  const applied = config?.applied_config;
  if (!applied) return null;

  const start = applied.start_chirp;
  const chirp = `${Math.round(start.from_hz)}→${Math.round(start.to_hz)}Hz`;

  return (
    <div className="flex items-center gap-1.5 rounded-md border bg-background-subtle px-2 py-1 text-[11px] text-muted">
      <span className="font-mono font-medium text-foreground">
        {applied.session_name}
      </span>
      <span className="h-3 w-px bg-border" />
      <span>chirp {chirp}</span>
      <span className="h-3 w-px bg-border" />
      <span>{applied.recording_mode}</span>
    </div>
  );
}
