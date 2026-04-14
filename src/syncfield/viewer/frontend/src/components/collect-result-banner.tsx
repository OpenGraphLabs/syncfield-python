import { cn } from "@/lib/utils";
import type { CollectResultEvent } from "@/hooks/use-session";

interface CollectResultBannerProps {
  result: CollectResultEvent;
  onDismiss: () => void;
}

/**
 * Banner shown after the user clicks "Collect Videos". Summarises how
 * many pending episodes were enqueued for aggregation and surfaces any
 * skipped manifests with their reason.
 */
export function CollectResultBanner({
  result,
  onDismiss,
}: CollectResultBannerProps) {
  const enqueuedCount = result.enqueued?.length ?? 0;
  const skippedCount = result.skipped?.length ?? 0;

  const hasError = !result.ok || !!result.error;
  const isEmpty = result.ok && enqueuedCount === 0 && skippedCount === 0;

  return (
    <div
      className={cn(
        "flex items-center justify-between gap-3 border-b px-4 py-2 text-sm",
        hasError && "border-destructive/40 bg-destructive/5 text-destructive",
        !hasError && isEmpty && "bg-muted/20 text-muted",
        !hasError && !isEmpty && "bg-success/10 text-success",
      )}
      role="status"
      aria-live="polite"
    >
      <div className="flex flex-col gap-0.5">
        {hasError ? (
          <span className="font-medium">
            Collect failed: {result.error ?? "unknown error"}
          </span>
        ) : isEmpty ? (
          <span className="font-medium">
            Nothing pending — no aggregation manifests found.
          </span>
        ) : (
          <span className="font-medium">
            Queued {enqueuedCount} episode{enqueuedCount === 1 ? "" : "s"} for video collection.
            {skippedCount > 0 && (
              <span className="ml-2 text-muted font-normal">
                ({skippedCount} skipped)
              </span>
            )}
          </span>
        )}
        {result.skipped && result.skipped.length > 0 && (
          <span className="text-xs text-muted">
            Skipped:{" "}
            {result.skipped
              .slice(0, 3)
              .map((s) => s.episode_id ?? s.path ?? "?")
              .join(", ")}
            {result.skipped.length > 3 && ", …"}
          </span>
        )}
      </div>

      <button
        type="button"
        onClick={onDismiss}
        className="rounded-md px-2 py-0.5 text-xs text-muted transition-colors hover:bg-foreground/5 hover:text-foreground"
      >
        Dismiss
      </button>
    </div>
  );
}
