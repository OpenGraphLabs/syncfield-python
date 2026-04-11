import type { SyncReport, SyncStreamResult } from "@/lib/review-types";
import { syncGrade } from "@/lib/review-types";
import { cn } from "@/lib/utils";

interface SyncQualityPanelProps {
  report: SyncReport | null;
  streams: string[];
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
        <ul className="space-y-1.5">
          {streams.map((sid) => {
            const streamResult = report?.streams[sid];
            return (
              <StreamRow key={sid} streamId={sid} result={streamResult} />
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
}: {
  streamId: string;
  result?: SyncStreamResult;
}) {
  if (!result) {
    return (
      <li className="flex items-center gap-2 text-xs">
        <span className="h-2 w-2 rounded-full bg-muted" />
        <span className="font-mono">{streamId}</span>
      </li>
    );
  }

  const grade = syncGrade(result);
  const isPrimary = result.role === "primary";

  return (
    <li className="flex items-center gap-2 text-xs">
      <span
        className={cn(
          "h-2 w-2 rounded-full",
          isPrimary ? "bg-primary" : GRADE_COLORS[grade]?.replace("text-", "bg-") ?? "bg-muted",
        )}
      />
      <span className="font-mono">{streamId}</span>
      {isPrimary ? (
        <span className="text-[10px] text-muted">(REF)</span>
      ) : (
        <>
          <span className="font-mono text-muted">
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
