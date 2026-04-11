import type { SyncReport, SyncStreamResult } from "@/lib/review-types";
import { syncGrade } from "@/lib/review-types";
import { cn } from "@/lib/utils";

interface SyncQualityPanelProps {
  report: SyncReport | null;
  streams: string[];
  primaryStream?: string;
  onStreamClick?: (streamId: string) => void;
}

const GRADE_COLORS: Record<string, string> = {
  excellent: "text-success",
  good: "text-primary",
  fair: "text-warning",
  poor: "text-destructive",
  primary: "text-muted",
};

const GRADE_BG: Record<string, string> = {
  excellent: "bg-success/10",
  good: "bg-primary/10",
  fair: "bg-warning/10",
  poor: "bg-destructive/10",
  primary: "bg-foreground/5",
};

export function SyncQualityPanel({
  report,
  streams,
  primaryStream,
  onStreamClick,
}: SyncQualityPanelProps) {
  return (
    <div className="space-y-4 p-4">
      {/* Sync quality section */}
      {report ? (
        <SyncedInfo report={report} />
      ) : (
        <div className="text-xs text-muted">
          Not yet synchronized. Click Sync to process.
        </div>
      )}

      {/* Divider */}
      <div className="h-px bg-border" />

      {/* Stream list */}
      <div>
        <h4 className="mb-2 text-[10px] font-medium uppercase tracking-wider text-muted">
          Streams
        </h4>
        <ul className="space-y-1">
          {streams.map((sid) => {
            const streamResult = report?.streams[sid];
            const isPrimary = sid === primaryStream;
            const isClickable = !isPrimary && onStreamClick != null && report != null;
            return (
              <StreamRow
                key={sid}
                streamId={sid}
                result={streamResult}
                isClickable={isClickable}
                onClick={isClickable ? () => onStreamClick(sid) : undefined}
              />
            );
          })}
        </ul>
      </div>

      {/* Divider */}
      <div className="h-px bg-border" />

      {/* Metadata */}
      {report && (
        <div>
          <h4 className="mb-2 text-[10px] font-medium uppercase tracking-wider text-muted">
            Metadata
          </h4>
          <dl className="space-y-1 text-xs">
            <MetaRow
              label="Duration"
              value={`${report.summary.synced_duration_sec.toFixed(1)}s`}
            />
            <MetaRow
              label="Synced frames"
              value={report.summary.total_synced_frames.toLocaleString()}
            />
            <MetaRow
              label="FPS"
              value={report.summary.actual_mean_fps.toFixed(1)}
            />
            <MetaRow label="Host" value={report.summary.primary_host} />
            <MetaRow
              label="Max drift"
              value={`${report.summary.max_drift_ms.toFixed(1)} ms`}
            />
          </dl>
        </div>
      )}
    </div>
  );
}

function SyncedInfo({ report }: { report: SyncReport }) {
  const overallGrade = deriveOverallGrade(report);

  return (
    <div>
      <h4 className="mb-2 text-[10px] font-medium uppercase tracking-wider text-muted">
        Sync Quality
      </h4>
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "rounded-md px-2 py-0.5 text-xs font-semibold capitalize",
            GRADE_BG[overallGrade],
            GRADE_COLORS[overallGrade],
          )}
        >
          {overallGrade}
        </span>
        <span className="text-xs text-muted">
          {report.summary.status === "success" ? "All streams aligned" : "Partial alignment"}
        </span>
      </div>
    </div>
  );
}

function StreamRow({
  streamId,
  result,
  isClickable,
  onClick,
}: {
  streamId: string;
  result?: SyncStreamResult;
  isClickable?: boolean;
  onClick?: () => void;
}) {
  if (!result) {
    return (
      <li className="flex items-center gap-2 rounded-md px-2 py-1.5 text-xs">
        <span className="h-2 w-2 rounded-full bg-muted" />
        <span className="font-mono">{streamId}</span>
      </li>
    );
  }

  const grade = syncGrade(result);
  const isPrimary = result.role === "primary";

  const content = (
    <>
      <span
        className={cn(
          "h-2 w-2 shrink-0 rounded-full",
          isPrimary
            ? "bg-blue-500"
            : GRADE_COLORS[grade]?.replace("text-", "bg-") ?? "bg-muted",
        )}
      />
      <span className="font-mono">{streamId}</span>
      {isPrimary ? (
        <span className="text-[10px] text-blue-500">REF</span>
      ) : (
        <>
          <span className="ml-auto font-mono text-muted">
            {result.offset_ms != null
              ? `${result.offset_ms > 0 ? "+" : ""}${result.offset_ms.toFixed(1)}ms`
              : ""}
          </span>
          {result.confidence != null && (
            <span className={cn("text-[10px]", GRADE_COLORS[grade])}>
              {Math.round(result.confidence * 100)}%
            </span>
          )}
        </>
      )}
      {isClickable && (
        <svg
          width="10"
          height="10"
          viewBox="0 0 16 16"
          fill="none"
          className="ml-auto shrink-0 text-muted"
        >
          <path
            d="M6 4L10 8L6 12"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      )}
    </>
  );

  return (
    <li
      className={cn(
        "flex items-center gap-2 rounded-md px-2 py-1.5 text-xs",
        isClickable &&
          "cursor-pointer transition-colors hover:bg-foreground/5",
      )}
      onClick={onClick}
    >
      {content}
    </li>
  );
}

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between">
      <dt className="text-muted">{label}</dt>
      <dd className="font-mono">{value}</dd>
    </div>
  );
}

function deriveOverallGrade(report: SyncReport): string {
  const secondaryStreams = Object.values(report.streams).filter(
    (s) => s.role !== "primary",
  );
  if (secondaryStreams.length === 0) return "primary";

  const confidences = secondaryStreams
    .map((s) => s.confidence ?? 0)
    .filter((c) => c > 0);
  if (confidences.length === 0) return "fair";

  const avg = confidences.reduce((a, b) => a + b, 0) / confidences.length;
  if (avg >= 0.8) return "excellent";
  if (avg >= 0.6) return "good";
  if (avg >= 0.4) return "fair";
  return "poor";
}
