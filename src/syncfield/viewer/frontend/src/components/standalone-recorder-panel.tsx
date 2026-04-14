import { useMemo } from "react";
import type { AggregationActiveJob, AggregationState } from "@/lib/types";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Prop types
// ---------------------------------------------------------------------------

export interface StandaloneRecorderStream {
  id: string;
  /** Session-level state (from SessionSnapshot.state). */
  sessionState: string;
  frame_count: number;
}

export interface StandaloneRecorderPanelProps {
  stream: StandaloneRecorderStream;
  aggregation: AggregationActiveJob | null;
  onRetry?: () => void;
}

// ---------------------------------------------------------------------------
// Derived status
// ---------------------------------------------------------------------------

type StatusKind =
  | "connecting"
  | "connected"
  | "recording"
  | "aggregating"
  | "ready"
  | "failed"
  | "idle";

interface DerivedStatus {
  kind: StatusKind;
  dot: "rec" | "agg" | "ok" | "fail" | "idle" | "connecting";
  label: string;
  // recording
  frameCount?: number;
  // aggregating
  currentBytes?: number;
  totalBytes?: number;
  camerasDone?: number;
  camerasTotal?: number;
}

function deriveStatus(
  stream: StandaloneRecorderStream,
  agg: AggregationActiveJob | null,
): DerivedStatus {
  // Order matters: active in-session states (recording, aggregating, failed)
  // take precedence over steady-state indicators (connected/ready, connecting).
  if (stream.sessionState === "recording") {
    return {
      kind: "recording",
      dot: "rec",
      label: "Recording",
      frameCount: stream.frame_count,
    };
  }
  if (agg?.state === "running") {
    const STAGE_LABELS: Record<string, string> = {
      starting: "starting…",
      switching_wifi: "switching WiFi…",
      probing: "probing camera…",
      downloading: "downloading",
      restoring_wifi: "restoring WiFi…",
    };
    return {
      kind: "aggregating",
      dot: "agg",
      label: (agg.stage && STAGE_LABELS[agg.stage]) ?? "Aggregating",
      currentBytes: agg.current_bytes,
      totalBytes: agg.current_total_bytes,
      camerasDone: agg.cameras_done,
      camerasTotal: agg.cameras_total,
    };
  }
  if (agg?.state === "failed") {
    return { kind: "failed", dot: "fail", label: "Failed" };
  }
  if (agg?.state === "completed") {
    return { kind: "ready", dot: "ok", label: "Ready" };
  }
  if (agg?.state === "pending") {
    return { kind: "idle", dot: "idle", label: "Pending aggregation" };
  }
  // Session-level connection state — the Go3S BLE handshake takes 3–7 s and
  // the user needs unambiguous feedback that it's in progress vs complete.
  if (
    stream.sessionState === "connecting" ||
    stream.sessionState === "countdown"
  ) {
    return {
      kind: "connecting",
      dot: "connecting",
      label: "Connecting",
    };
  }
  if (stream.sessionState === "connected") {
    return {
      kind: "connected",
      dot: "ok",
      label: "Connected",
    };
  }
  return { kind: "idle", dot: "idle", label: "Idle" };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function StandaloneRecorderPanel({
  stream,
  aggregation,
  onRetry,
}: StandaloneRecorderPanelProps) {
  const status = useMemo(
    () => deriveStatus(stream, aggregation),
    [stream, aggregation],
  );

  return (
    <div className="flex min-h-[180px] flex-col items-center justify-center gap-3 px-4 py-6">
      <CameraGlyph className="text-muted-foreground opacity-30" />

      <div className="text-center">
        <p className="text-sm font-medium text-foreground">Standalone recorder</p>
        <p className="mt-0.5 text-xs text-muted">Live preview unavailable</p>
      </div>

      <StatusRow status={status} onRetry={onRetry} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Status row
// ---------------------------------------------------------------------------

function StatusRow({
  status,
  onRetry,
}: {
  status: DerivedStatus;
  onRetry?: () => void;
}) {
  const dotClass = cn(
    "inline-block h-2 w-2 shrink-0 rounded-full",
    {
      "bg-recording animate-pulse-recording": status.dot === "rec",
      "bg-warning animate-pulse-recording":
        status.dot === "agg" || status.dot === "connecting",
      "bg-success": status.dot === "ok",
      "bg-destructive": status.dot === "fail",
      "bg-muted": status.dot === "idle",
    },
  );

  return (
    <div className="flex items-center gap-1.5 text-xs">
      <span className={dotClass} aria-hidden="true" />
      <span aria-label={`Status: ${status.label}`}>
        {status.kind === "recording" && (
          <span className="text-recording font-medium">
            Recording
            {status.frameCount !== undefined && status.frameCount > 0
              ? ` · ${status.frameCount} frames`
              : ""}
          </span>
        )}
        {status.kind === "aggregating" && (
          <span className="text-warning font-medium">
            {status.label}
            {status.totalBytes
              ? ` · ${formatBytes(status.currentBytes ?? 0)} / ${formatBytes(status.totalBytes)}`
              : status.currentBytes
                ? ` · ${formatBytes(status.currentBytes)}`
                : ""}
          </span>
        )}
        {status.kind === "connecting" && (
          <span className="text-warning font-medium">Connecting to BLE…</span>
        )}
        {status.kind === "connected" && (
          <span className="text-success font-medium">Connected · ready to record</span>
        )}
        {status.kind === "ready" && (
          <span className="text-success font-medium">Ready · file downloaded</span>
        )}
        {status.kind === "failed" && (
          <span className="flex items-center gap-1.5">
            <span className="text-destructive font-medium">Failed</span>
            {onRetry && (
              <button
                type="button"
                onClick={onRetry}
                className="rounded-md bg-destructive/10 px-2 py-0.5 text-[11px] font-medium text-destructive transition-colors hover:bg-destructive/20"
              >
                Retry
              </button>
            )}
          </span>
        )}
        {status.kind === "idle" && (
          <span className="text-muted">{status.label}</span>
        )}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Camera SVG glyph
// ---------------------------------------------------------------------------

function CameraGlyph({ className }: { className?: string }) {
  return (
    <svg
      width="40"
      height="40"
      viewBox="0 0 24 24"
      aria-hidden="true"
      className={className}
    >
      <path
        fill="currentColor"
        d="M9 4L7.5 6H4a2 2 0 00-2 2v10a2 2 0 002 2h16a2 2 0 002-2V8a2 2 0 00-2-2h-3.5L15 4H9zm3 5a4 4 0 110 8 4 4 0 010-8z"
      />
    </svg>
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

// Re-export AggregationState so callers don't need a separate import.
export type { AggregationState };
