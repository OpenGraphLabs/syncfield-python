import type { AggregationSnapshotWS, StreamSnapshot } from "@/lib/types";
import { formatCount, formatHz } from "@/lib/format";
import { cn } from "@/lib/utils";
import { AudioLevelChart } from "./audio-level-chart";
import { VideoPreview } from "./video-preview";
import { SensorChart } from "./sensor-chart";
import {
  StandaloneRecorderPanel,
  type StandaloneRecorderStream,
} from "./standalone-recorder-panel";

interface StreamCardProps {
  stream: StreamSnapshot;
  canRemove: boolean;
  onRemove: (streamId: string) => void;
  /** Session state string — forwarded to StandaloneRecorderPanel for recording detection. */
  sessionState?: string;
  /** Top-level aggregation snapshot from the WS payload — used by StandaloneRecorderPanel. */
  aggregation?: AggregationSnapshotWS;
  /** Callback to send an aggregation retry command for a given job ID. */
  onRetryAggregation?: (jobId: string) => void;
}

/**
 * Per-stream card with variant body by kind.
 *
 * - **video (live_preview=false)** — StandaloneRecorderPanel (Go3S, etc.)
 * - **video** — MJPEG preview via `<img>`
 * - **sensor** — Real-time SVG line chart via SSE
 * - **audio / custom** — Minimal stats placeholder
 */
export function StreamCard({
  stream,
  canRemove,
  onRemove,
  sessionState,
  aggregation,
  onRetryAggregation,
}: StreamCardProps) {
  // Dispatch to StandaloneRecorderPanel for video streams without live preview
  // (e.g. Insta360 Go3S which downloads files via BLE/Wi-Fi after recording).
  const isStandalone =
    stream.kind === "video" && stream.capabilities?.live_preview === false;

  if (isStandalone) {
    const standaloneStream: StandaloneRecorderStream = {
      id: stream.id,
      sessionState: sessionState ?? "idle",
      frame_count: stream.frame_count,
    };
    const activeJob = mapAggregationForStream(aggregation, stream.id);
    return (
      <div className="flex min-w-[320px] flex-1 flex-col overflow-hidden rounded-xl border bg-card">
        {/* Card header */}
        <div className="flex items-center gap-2 px-4 py-2.5">
          <span
            className={cn(
              "inline-block h-2.5 w-2.5 shrink-0 rounded-full",
              sessionState === "recording"
                ? "bg-recording animate-pulse-recording"
                : "bg-muted",
            )}
          />
          <span className="truncate font-mono text-sm font-medium">
            {stream.id}
          </span>
          <div className="flex-1" />
          {canRemove && (
            <button
              onClick={() => onRemove(stream.id)}
              className="rounded-md p-1 text-muted transition-colors hover:bg-foreground/5 hover:text-destructive"
              title={`Remove ${stream.id}`}
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path
                  d="M4 4L12 12M12 4L4 12"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                />
              </svg>
            </button>
          )}
        </div>

        {/* Tags */}
        <div className="flex gap-1.5 px-4 pb-2">
          <Tag>{stream.kind}</Tag>
          <Tag>standalone</Tag>
          {stream.produces_file && <Tag>file</Tag>}
        </div>

        {/* Body */}
        <div className="flex-1 border-t">
          <StandaloneRecorderPanel
            stream={standaloneStream}
            aggregation={activeJob}
            onRetry={
              activeJob?.job_id && onRetryAggregation
                ? () => onRetryAggregation(activeJob.job_id)
                : undefined
            }
          />
        </div>

        {/* Footer stats */}
        <div className="flex items-center gap-3 border-t px-4 py-2.5 text-xs text-muted">
          <span className="font-mono">{formatCount(stream.frame_count)}</span>
          <span className="h-3 w-px bg-border" />
          <span className="font-mono">{formatHz(stream.effective_hz)}</span>
          {stream.problem_count > 0 && (
            <>
              <span className="h-3 w-px bg-border" />
              <span className="text-destructive">
                {stream.problem_count} issue{stream.problem_count > 1 ? "s" : ""}
              </span>
            </>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-w-[320px] flex-1 flex-col overflow-hidden rounded-xl border bg-card">
      {/* Card header */}
      <div className="flex items-center gap-2 px-4 py-2.5">
        <span
          className={cn(
            "inline-block h-2.5 w-2.5 shrink-0 rounded-full",
            stream.effective_hz > 0 ? "bg-success" : "bg-muted",
          )}
        />
        <span className="truncate font-mono text-sm font-medium">
          {stream.id}
        </span>
        <div className="flex-1" />
        {canRemove && (
          <button
            onClick={() => onRemove(stream.id)}
            className="rounded-md p-1 text-muted transition-colors hover:bg-foreground/5 hover:text-destructive"
            title={`Remove ${stream.id}`}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path
                d="M4 4L12 12M12 4L4 12"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
              />
            </svg>
          </button>
        )}
      </div>

      {/* Tags */}
      <div className="flex gap-1.5 px-4 pb-2">
        <Tag>{stream.kind}</Tag>
        {stream.provides_audio_track && stream.kind !== "audio" && <Tag>audio</Tag>}
        {stream.produces_file && <Tag>file</Tag>}
      </div>

      {/* Body — varies by stream kind */}
      <div className="flex-1 border-t">
        {stream.kind === "video" ? (
          <VideoPreview streamId={stream.id} />
        ) : stream.kind === "audio" ? (
          <AudioLevelChart streamId={stream.id} />
        ) : stream.kind === "sensor" ? (
          <SensorChart streamId={stream.id} />
        ) : (
          <div className="flex h-full min-h-[180px] items-center justify-center text-xs text-muted">
            No preview
          </div>
        )}
      </div>

      {/* Footer stats */}
      <div className="flex items-center gap-3 border-t px-4 py-2.5 text-xs text-muted">
        <span className="font-mono">{formatCount(stream.frame_count)}</span>
        <span className="h-3 w-px bg-border" />
        <span className="font-mono">{formatHz(stream.effective_hz)}</span>
        {stream.problem_count > 0 && (
          <>
            <span className="h-3 w-px bg-border" />
            <span className="text-destructive">
              {stream.problem_count} issue{stream.problem_count > 1 ? "s" : ""}
            </span>
          </>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helper — pick the aggregation job relevant to a specific stream
// ---------------------------------------------------------------------------

/**
 * Returns the active aggregation job if it involves the given stream, or the
 * most-recent job for that stream from recent_jobs. Falls back to the active
 * job unconditionally when the stream_id is unavailable (older server).
 */
function mapAggregationForStream(
  agg: AggregationSnapshotWS | undefined,
  streamId: string,
) {
  if (!agg) return null;
  const job = agg.active_job;
  if (job) {
    // If server reports a specific stream_id for the running camera, match it;
    // otherwise surface the active job on all standalone streams.
    if (!job.current_stream_id || job.current_stream_id === streamId) {
      return job;
    }
  }
  // Check recent_jobs for a completed / failed job touching this stream.
  for (const rj of agg.recent_jobs) {
    if (!rj.current_stream_id || rj.current_stream_id === streamId) {
      return rj;
    }
  }
  return null;
}

function Tag({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded-md bg-foreground/5 px-2 py-0.5 text-[11px] font-medium text-muted">
      {children}
    </span>
  );
}
