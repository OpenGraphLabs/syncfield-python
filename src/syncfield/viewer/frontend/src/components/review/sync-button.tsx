import type { SyncJobStatus } from "@/lib/review-types";
import { cn } from "@/lib/utils";

interface SyncButtonProps {
  jobStatus: SyncJobStatus | null;
  isSyncing: boolean;
  hasSyncReport: boolean;
  onSync: () => void;
}

export function SyncButton({
  jobStatus,
  isSyncing,
  hasSyncReport,
  onSync,
}: SyncButtonProps) {
  if (isSyncing && jobStatus) {
    const pct = Math.round(jobStatus.progress * 100);
    return (
      <div className="flex items-center gap-2">
        <div className="h-1.5 w-24 overflow-hidden rounded-full bg-foreground/10">
          <div
            className="h-full rounded-full bg-primary transition-all"
            style={{ width: `${pct}%` }}
          />
        </div>
        <span className="text-[10px] font-medium text-muted">
          {jobStatus.phase} · {pct}%
        </span>
      </div>
    );
  }

  return (
    <button
      onClick={onSync}
      disabled={isSyncing}
      className={cn(
        "rounded-lg px-3 py-1 text-xs font-medium transition-colors",
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
