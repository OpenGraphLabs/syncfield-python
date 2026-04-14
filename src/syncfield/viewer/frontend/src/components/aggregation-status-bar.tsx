import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AggregationActiveDisplay {
  jobId: string;
  episodeId: string;
  state: "running" | "failed";
  currentStreamId: string | null;
  currentBytes: number;
  totalBytes: number;
  camerasDone: number;
  camerasTotal: number;
  stage: string | null;
}

const STAGE_LABELS: Record<string, string> = {
  starting: "Starting…",
  switching_wifi: "Connecting to camera WiFi…",
  probing: "Reaching camera…",
  downloading: "Downloading video",
  restoring_wifi: "Restoring your WiFi…",
};

const STAGE_HINTS: Record<string, string> = {
  switching_wifi:
    "Tap the camera screen to wake it if this takes more than ~15 s.",
  probing: "Waiting for the camera's HTTP server…",
  downloading: "",
  restoring_wifi: "Putting you back on your original network.",
};

function stageLabel(stage: string | null): string | null {
  if (!stage) return null;
  return STAGE_LABELS[stage] ?? stage;
}

interface AggregationStatusBarProps {
  active: AggregationActiveDisplay | null;
  onRetry: (jobId: string) => void;
  onViewDetails?: (episodeId: string) => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function AggregationStatusBar({
  active,
  onRetry,
  onViewDetails,
}: AggregationStatusBarProps) {
  const stageForEffect = active?.stage ?? null;
  const jobIdForEffect = active?.jobId ?? null;
  const [elapsedSec, setElapsedSec] = useState(0);
  const stageStartRef = useRef<number>(Date.now());

  // Reset the per-stage timer whenever the job id OR stage changes,
  // so the "Xs" we display is time-in-current-stage, not total wall
  // time — much more informative when a job hangs on switching_wifi
  // but moves quickly once downloading starts.
  useEffect(() => {
    stageStartRef.current = Date.now();
    setElapsedSec(0);
  }, [stageForEffect, jobIdForEffect]);

  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => {
      setElapsedSec(Math.floor((Date.now() - stageStartRef.current) / 1000));
    }, 500);
    return () => clearInterval(t);
  }, [active]);

  if (!active) return null;

  const pct = active.totalBytes
    ? Math.round((active.currentBytes / active.totalBytes) * 100)
    : 0;

  if (active.state === "failed") {
    return (
      <div
        className={cn(
          "flex items-center justify-between gap-3 border-b bg-destructive/5 px-4 py-2",
          "text-xs text-destructive",
        )}
        role="status"
        aria-live="polite"
      >
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2 shrink-0 rounded-full bg-destructive"
            aria-hidden="true"
          />
          <span>
            Aggregation failed for{" "}
            <code className="font-mono font-medium">{active.episodeId}</code>
          </span>
        </div>
        <div className="flex items-center gap-2">
          {onViewDetails && (
            <button
              type="button"
              onClick={() => onViewDetails(active.episodeId)}
              className="rounded-md px-2 py-0.5 text-[11px] font-medium text-destructive/70 transition-colors hover:text-destructive"
            >
              View Details
            </button>
          )}
          <button
            type="button"
            onClick={() => onRetry(active.jobId)}
            className="rounded-md bg-destructive/10 px-2 py-0.5 text-[11px] font-medium text-destructive transition-colors hover:bg-destructive/20"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  // Running state
  const stageText = stageLabel(active.stage);
  const stageHint = active.stage ? STAGE_HINTS[active.stage] ?? "" : "";
  const isDownloading = active.stage === "downloading";
  // Red flag: switching_wifi stuck over 25 s → surface a prompt.
  const switchingTooLong =
    active.stage === "switching_wifi" && elapsedSec >= 25;
  return (
    <div
      className="flex items-center gap-3 border-b bg-warning/5 px-4 py-2 text-xs"
      role="status"
      aria-live="polite"
    >
      {/* Animated dot */}
      <span
        className="inline-block h-2 w-2 shrink-0 rounded-full bg-warning animate-pulse-recording"
        aria-hidden="true"
      />

      {/* Description */}
      <span className="text-warning font-medium">
        Aggregating{" "}
        <code className="font-mono">{active.episodeId}</code>
        {active.currentStreamId && (
          <span className="text-muted font-normal">
            {" "}
            · {active.currentStreamId}
          </span>
        )}
        {active.camerasTotal > 0 && (
          <span className="text-muted font-normal">
            {" "}
            ({active.camerasDone}/{active.camerasTotal})
          </span>
        )}
      </span>

      {/* Stage label + elapsed — shown during pre-download phases where
          byte progress would otherwise be 0% and the user can't tell
          whether anything is happening. */}
      {stageText && !isDownloading && (
        <span className="text-muted italic tabular-nums">
          {stageText} ({elapsedSec}s)
        </span>
      )}

      {/* Progress bar — meaningful only during the download phase when
          totalBytes is known. In pre-download phases, show an
          indeterminate-style pulsing bar instead of a fixed 0%. */}
      <div
        className="h-1.5 w-24 shrink-0 overflow-hidden rounded-full bg-warning/20"
        aria-hidden="true"
      >
        {isDownloading && active.totalBytes > 0 ? (
          <div
            className="h-full rounded-full bg-warning transition-all duration-300"
            style={{ width: `${pct}%` }}
          />
        ) : (
          <div className="h-full w-full animate-pulse-recording rounded-full bg-warning/60" />
        )}
      </div>

      {/* Bytes / percent — only during download phase. */}
      {isDownloading && active.totalBytes > 0 ? (
        <span className="tabular-nums text-muted">
          {pct}% · {formatBytes(active.currentBytes)} /{" "}
          {formatBytes(active.totalBytes)}
        </span>
      ) : isDownloading ? (
        <span className="tabular-nums text-muted">
          {formatBytes(active.currentBytes)} downloaded
        </span>
      ) : null}

      {/* Contextual hint (e.g. "tap camera screen to wake it") — only
          surfaces during the phase that could stall. Elevated visibility
          once switching has lingered past ~25 s. */}
      {stageHint && !isDownloading && (
        <span
          className={cn(
            "hidden md:inline text-[11px]",
            switchingTooLong
              ? "text-destructive font-medium"
              : "text-muted italic",
          )}
        >
          {switchingTooLong
            ? "Camera still hasn't accepted the connection — tap its screen to wake it."
            : stageHint}
        </span>
      )}

      {/* Optional View Details */}
      {onViewDetails && (
        <button
          type="button"
          onClick={() => onViewDetails(active.episodeId)}
          className="ml-auto rounded-md px-2 py-0.5 text-[11px] font-medium text-muted transition-colors hover:text-foreground"
        >
          View Details
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helper: map WS snake_case active_job → AggregationActiveDisplay
// ---------------------------------------------------------------------------

import type { AggregationSnapshotWS } from "@/lib/types";

/**
 * Maps the WS aggregation snapshot into the display type expected by
 * AggregationStatusBar. Returns null when there is no active job or the job
 * is already completed.
 */
export function mapActiveAggregation(
  aggregation: AggregationSnapshotWS | undefined,
): AggregationActiveDisplay | null {
  const job = aggregation?.active_job;
  if (!job) return null;
  // Only show running or failed — completed jobs are done
  if (job.state !== "running" && job.state !== "failed") return null;

  return {
    jobId: job.job_id,
    episodeId: job.episode_id,
    state: job.state,
    currentStreamId: job.current_stream_id,
    currentBytes: job.current_bytes,
    totalBytes: job.current_total_bytes,
    camerasDone: job.cameras_done,
    camerasTotal: job.cameras_total,
    stage: job.stage ?? null,
  };
}

// ---------------------------------------------------------------------------
// Aggregation badge — for episode list rows
// ---------------------------------------------------------------------------

interface AggregationBadgeProps {
  state?: string;
  percent?: number;
}

/**
 * Small inline badge showing aggregation state for an episode.
 * Use in episode list rows to surface per-episode aggregation status.
 */
export function AggregationBadge({ state, percent }: AggregationBadgeProps) {
  if (!state || state === "completed") {
    return (
      <span className="rounded-md bg-success/10 px-2 py-0.5 text-[10px] font-medium text-success">
        Ready
      </span>
    );
  }
  if (state === "running") {
    return (
      <span className="rounded-md bg-warning/10 px-2 py-0.5 text-[10px] font-medium text-warning">
        Aggregating {percent ?? 0}%
      </span>
    );
  }
  if (state === "failed") {
    return (
      <span className="rounded-md bg-destructive/10 px-2 py-0.5 text-[10px] font-medium text-destructive">
        Failed
      </span>
    );
  }
  // pending / unknown
  return (
    <span className="rounded-md bg-foreground/5 px-2 py-0.5 text-[10px] font-medium text-muted">
      Pending
    </span>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}
