import type { DriftData } from "@/hooks/use-drift-data";

interface DriftChartProps {
  data: DriftData | null;
  isLoading: boolean;
}

/**
 * Before/After drift chart.
 *
 * Shows how much the sync correction improved stream alignment:
 * - Gray dashed = estimated raw drift before correction
 * - Green solid = residual drift after correction
 * - Shaded area = improvement
 */
export function DriftChart({ data, isLoading }: DriftChartProps) {
  if (isLoading) {
    return <Placeholder>Loading drift data…</Placeholder>;
  }

  if (!data || data.timesSec.length === 0) {
    return <Placeholder>No drift data available</Placeholder>;
  }

  const { timesSec, beforeDrift, afterDrift, meanAfterMs, improvementPct } =
    data;
  const n = timesSec.length;

  // Y-axis bounds (in ms)
  const yMax = Math.max(...beforeDrift, ...afterDrift, 1);

  // SVG coordinate system
  const W = 800;
  const H = 100;
  const PAD = { top: 8, right: 12, bottom: 18, left: 40 };
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;

  const x = (i: number) => PAD.left + (i / Math.max(n - 1, 1)) * plotW;
  const y = (v: number) => PAD.top + plotH - (v / yMax) * plotH;

  // Build polyline points (downsample if too many points)
  const step = Math.max(1, Math.floor(n / 400));
  const beforePts: string[] = [];
  const afterPts: string[] = [];
  for (let i = 0; i < n; i += step) {
    beforePts.push(`${x(i)},${y(beforeDrift[i]!)}`);
    afterPts.push(`${x(i)},${y(afterDrift[i]!)}`);
  }
  // Always include last point
  if ((n - 1) % step !== 0) {
    beforePts.push(`${x(n - 1)},${y(beforeDrift[n - 1]!)}`);
    afterPts.push(`${x(n - 1)},${y(afterDrift[n - 1]!)}`);
  }

  const beforeLine = `M${beforePts.join("L")}`;

  // Fill area between before and after
  const fillArea = `${beforeLine}L${[...afterPts].reverse().join("L")}Z`;

  // Time axis labels
  const totalSec = timesSec[n - 1] ?? 0;

  return (
    <div className="px-4 py-2">
      {/* Header */}
      <div className="mb-1 flex items-center gap-4 text-[10px]">
        <span className="font-medium uppercase tracking-wider text-muted">
          Sync Drift
        </span>
        <div className="flex items-center gap-3 text-muted">
          <span className="flex items-center gap-1">
            <span className="inline-block h-px w-3 border-t border-dashed border-foreground/30" />
            Before
          </span>
          <span className="flex items-center gap-1">
            <span className="inline-block h-0.5 w-3 rounded-full bg-primary" />
            After ({meanAfterMs.toFixed(1)} ms avg)
          </span>
        </div>
        {improvementPct > 0 && (
          <span className="ml-auto font-medium text-success">
            ↓ {improvementPct.toFixed(0)}% improved
          </span>
        )}
      </div>

      {/* Chart */}
      <svg viewBox={`0 0 ${W} ${H}`} className="h-24 w-full">
        {/* Y-axis labels */}
        <text
          x={PAD.left - 4}
          y={PAD.top + 6}
          textAnchor="end"
          fill="currentColor"
          opacity={0.35}
          fontSize={8}
        >
          {yMax >= 100 ? `${(yMax / 1000).toFixed(1)}s` : `${yMax.toFixed(0)}ms`}
        </text>
        <text
          x={PAD.left - 4}
          y={PAD.top + plotH}
          textAnchor="end"
          fill="currentColor"
          opacity={0.35}
          fontSize={8}
        >
          0
        </text>

        {/* X-axis labels */}
        <text
          x={PAD.left}
          y={H - 2}
          fill="currentColor"
          opacity={0.35}
          fontSize={8}
        >
          0s
        </text>
        <text
          x={W - PAD.right}
          y={H - 2}
          textAnchor="end"
          fill="currentColor"
          opacity={0.35}
          fontSize={8}
        >
          {totalSec.toFixed(0)}s
        </text>

        {/* Grid lines */}
        <line
          x1={PAD.left}
          y1={PAD.top}
          x2={W - PAD.right}
          y2={PAD.top}
          stroke="currentColor"
          strokeOpacity={0.06}
        />
        <line
          x1={PAD.left}
          y1={PAD.top + plotH}
          x2={W - PAD.right}
          y2={PAD.top + plotH}
          stroke="currentColor"
          strokeOpacity={0.06}
        />

        {/* Fill between */}
        <path d={fillArea} fill="currentColor" className="text-primary" fillOpacity={0.08} />

        {/* Before (gray dashed) */}
        <path
          d={beforeLine}
          fill="none"
          stroke="currentColor"
          strokeOpacity={0.2}
          strokeWidth={1}
          strokeDasharray="4,3"
        />

        {/* After (green solid) */}
        <path
          d={`M${afterPts.join("L")}`}
          fill="none"
          className="text-primary"
          stroke="currentColor"
          strokeWidth={1.5}
        />
      </svg>
    </div>
  );
}

function Placeholder({ children }: { children: string }) {
  return (
    <div className="flex h-24 items-center justify-center text-[10px] text-muted">
      {children}
    </div>
  );
}
