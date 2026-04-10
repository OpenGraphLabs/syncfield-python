import { CheckCircle2, AlertCircle } from "lucide-react";
import type { SyncReport, SyncStreamResult } from "../types";
import { qualityColor } from "../lib/quality";

function formatOffset(seconds: number): string {
  const sign = seconds >= 0 ? "+" : "";
  return `${sign}${seconds.toFixed(3)}s`;
}

function StreamCard({
  streamId,
  result,
}: {
  streamId: string;
  result: SyncStreamResult;
}) {
  const confidencePct = Math.round(result.confidence * 100);
  return (
    <div className="rounded-xl border border-zinc-200/80 bg-white px-4 py-3 min-w-[200px]">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[11px] font-mono text-zinc-600 truncate">
          {streamId}
        </span>
        <span
          className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${qualityColor(result.quality)}`}
        >
          {result.quality}
        </span>
      </div>
      <div className="font-mono text-sm text-zinc-800 tabular-nums">
        {formatOffset(result.offset_seconds)}
      </div>
      <div className="mt-2 flex items-center gap-2">
        <div className="h-1 flex-1 overflow-hidden rounded-full bg-zinc-100">
          <div
            className="h-full rounded-full bg-cyan-500"
            style={{ width: `${confidencePct}%` }}
          />
        </div>
        <span className="text-[10px] text-zinc-400 tabular-nums">
          {confidencePct}%
        </span>
      </div>
    </div>
  );
}

export default function SyncReportPanel({
  report,
}: {
  report: SyncReport | null;
}) {
  if (!report) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-amber-200 bg-amber-50/40 px-4 py-3">
        <AlertCircle size={16} className="text-amber-600" />
        <span className="text-xs text-amber-700">
          Sync not run yet — only Before view available
        </span>
      </div>
    );
  }

  const entries = Object.entries(report.streams);

  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <CheckCircle2 size={14} className="text-teal-600" />
        <span className="text-[11px] uppercase tracking-wider text-zinc-500">
          Sync report
        </span>
      </div>
      <div className="flex flex-wrap gap-2">
        {entries.map(([id, result]) => (
          <StreamCard key={id} streamId={id} result={result} />
        ))}
      </div>
    </div>
  );
}
