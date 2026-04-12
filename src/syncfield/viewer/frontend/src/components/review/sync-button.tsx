import type { SyncJobStatus } from "@/lib/review-types";
import { cn } from "@/lib/utils";

interface SyncButtonProps {
  jobStatus: SyncJobStatus | null;
  isSyncing: boolean;
  hasSyncReport: boolean;
  onSync: () => void;
}

const PHASE_LABELS: Record<string, string> = {
  ingest: "Analyzing streams",
  primary: "Selecting primary",
  audio_check: "Checking audio",
  align: "Aligning streams",
  frame_map: "Building frame map",
  reencode: "Re-encoding videos",
  report: "Generating report",
  done: "Finishing up",
};

export function SyncButton({
  jobStatus,
  isSyncing,
  hasSyncReport,
  onSync,
}: SyncButtonProps) {
  // Syncing in progress — show inline progress
  if (isSyncing) {
    const progress = jobStatus?.progress ?? 0;
    const pct = Math.round(progress * 100);
    const phase = jobStatus?.phase ?? "processing";
    const phaseLabel = PHASE_LABELS[phase] ?? phase;

    return (
      <div className="flex items-center gap-3">
        {/* Spinner */}
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        {/* Progress info */}
        <div className="flex flex-col items-end gap-0.5">
          <div className="flex items-center gap-2">
            <div className="h-2 w-32 overflow-hidden rounded-full bg-foreground/10">
              <div
                className="h-full rounded-full bg-primary transition-all duration-500"
                style={{ width: `${Math.max(pct, 3)}%` }}
              />
            </div>
            <span className="min-w-[2.5rem] text-right font-mono text-xs tabular-nums text-foreground">
              {pct}%
            </span>
          </div>
          <span className="text-[10px] text-muted">{phaseLabel}</span>
        </div>
      </div>
    );
  }

  return (
    <button
      onClick={onSync}
      disabled={isSyncing}
      className={cn(
        "rounded-lg px-3 py-1.5 text-xs font-medium transition-colors",
        hasSyncReport
          ? "border hover:bg-foreground/5"
          : "bg-primary text-primary-foreground hover:bg-primary/90",
        "disabled:opacity-40",
      )}
    >
      {hasSyncReport ? "Re-sync" : "Sync"}
    </button>
  );
}
