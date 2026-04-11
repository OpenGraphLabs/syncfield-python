import type { StopResultEvent } from "@/lib/types";
import { cn } from "@/lib/utils";

interface StopResultBannerProps {
  result: StopResultEvent;
  onDismiss: () => void;
}

/**
 * Banner shown after Stop — displays per-stream save status.
 *
 * - "saving" → spinner
 * - "success" → green banner with per-stream check marks
 * - "partial" → amber banner with per-stream status (some failed)
 * - "error" → red banner with error message
 */
export function StopResultBanner({ result, onDismiss }: StopResultBannerProps) {
  if (result.status === "saving") {
    return (
      <div className="flex items-center gap-3 border-b bg-foreground/3 px-4 py-3">
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        <span className="text-xs font-medium">Saving recording…</span>
      </div>
    );
  }

  const isSuccess = result.status === "success";
  const isError = result.status === "error";
  const streams = result.streams ?? {};

  return (
    <div
      className={cn(
        "border-b px-4 py-3",
        isSuccess && "border-success/20 bg-success/5",
        result.status === "partial" && "border-warning/20 bg-warning/5",
        isError && "border-destructive/20 bg-destructive/5",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          {/* Title */}
          <p
            className={cn(
              "text-xs font-medium",
              isSuccess && "text-success",
              result.status === "partial" && "text-warning",
              isError && "text-destructive",
            )}
          >
            {isSuccess && "Recording saved successfully"}
            {result.status === "partial" && "Recording saved with issues"}
            {isError && `Recording failed: ${result.error}`}
          </p>

          {/* Per-stream details */}
          {Object.keys(streams).length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
              {Object.entries(streams).map(([sid, info]) => (
                <StreamStatus key={sid} streamId={sid} info={info} />
              ))}
            </div>
          )}

          {/* Output path */}
          {result.output_dir && isSuccess && (
            <p className="mt-1 font-mono text-[10px] text-muted">
              {result.output_dir}
            </p>
          )}
        </div>

        {/* Dismiss */}
        <button
          onClick={onDismiss}
          className="shrink-0 rounded-md p-1 text-muted transition-colors hover:bg-foreground/5 hover:text-foreground"
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
            <path
              d="M4 4L12 12M12 4L4 12"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
            />
          </svg>
        </button>
      </div>
    </div>
  );
}

function StreamStatus({
  streamId,
  info,
}: {
  streamId: string;
  info: {
    status: string;
    frame_count: number;
    error?: string;
    warning?: string;
    file_exists?: boolean;
  };
}) {
  const ok = info.status === "completed" && info.file_exists !== false;
  const hasWarning = info.warning != null;

  return (
    <div className="flex items-center gap-1 text-[11px]">
      {ok && !hasWarning ? (
        <span className="text-success">✓</span>
      ) : ok && hasWarning ? (
        <span className="text-warning">⚠</span>
      ) : (
        <span className="text-destructive">✗</span>
      )}
      <span className="font-mono">{streamId}</span>
      <span className="text-muted">
        {info.frame_count.toLocaleString()} frames
      </span>
      {info.error && (
        <span className="text-destructive">— {info.error}</span>
      )}
      {hasWarning && !info.error && (
        <span className="text-warning">— {info.warning}</span>
      )}
    </div>
  );
}
