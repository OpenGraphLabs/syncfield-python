import type { StreamSnapshot } from "@/lib/types";
import { formatCount, formatHz, formatMsAgo } from "@/lib/format";
import { cn } from "@/lib/utils";
import { VideoPreview } from "./video-preview";
import { SensorChart } from "./sensor-chart";

interface StreamCardProps {
  stream: StreamSnapshot;
  canRemove: boolean;
  onRemove: (streamId: string) => void;
}

/**
 * Per-stream card with variant body by kind.
 *
 * - **video** — MJPEG preview via `<img>`
 * - **sensor** — Real-time SVG line chart via SSE
 * - **audio / custom** — Minimal stats placeholder
 */
export function StreamCard({ stream, canRemove, onRemove }: StreamCardProps) {
  return (
    <div className="w-[260px] shrink-0 overflow-hidden rounded-xl border bg-card">
      {/* Card header */}
      <div className="flex items-center gap-2 px-3 py-2">
        <span
          className={cn(
            "inline-block h-2 w-2 shrink-0 rounded-full",
            stream.effective_hz > 0 ? "bg-success" : "bg-muted",
          )}
        />
        <span className="truncate font-mono text-xs font-medium">
          {stream.id}
        </span>
        <div className="flex-1" />
        {canRemove && (
          <button
            onClick={() => onRemove(stream.id)}
            className="text-muted transition-colors hover:text-destructive"
            title={`Remove ${stream.id}`}
          >
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path
                d="M3.5 3.5L10.5 10.5M10.5 3.5L3.5 10.5"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
              />
            </svg>
          </button>
        )}
      </div>

      {/* Tags */}
      <div className="flex gap-1.5 px-3 pb-1">
        <Tag>{stream.kind}</Tag>
        {stream.provides_audio_track && <Tag>audio</Tag>}
        {stream.produces_file && <Tag>file</Tag>}
      </div>

      {/* Body — varies by stream kind */}
      <div className="border-t">
        {stream.kind === "video" ? (
          <VideoPreview streamId={stream.id} />
        ) : stream.kind === "sensor" ? (
          <SensorChart streamId={stream.id} />
        ) : (
          <div className="flex h-[146px] items-center justify-center text-xs text-muted">
            No preview
          </div>
        )}
      </div>

      {/* Footer stats */}
      <div className="flex items-center gap-3 border-t px-3 py-2 text-[11px] text-muted">
        <span>{formatCount(stream.frame_count)}</span>
        <span className="h-3 w-px bg-border" />
        <span>{formatHz(stream.effective_hz)}</span>
        <span className="h-3 w-px bg-border" />
        <span>{formatMsAgo(stream.last_sample_ms_ago)}</span>
        {stream.health_count > 0 && (
          <>
            <span className="h-3 w-px bg-border" />
            <span className="text-destructive">
              {stream.health_count} event{stream.health_count > 1 ? "s" : ""}
            </span>
          </>
        )}
      </div>
    </div>
  );
}

function Tag({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded-md bg-foreground/5 px-1.5 py-0.5 text-[10px] font-medium text-muted">
      {children}
    </span>
  );
}
