import type { DriftData } from "@/hooks/use-drift-data";

interface DriftChartProps {
  data: DriftData | null;
  isLoading: boolean;
}

const CHART_H = 80;
const PADDING = { top: 12, right: 12, bottom: 20, left: 40 };

/**
 * Before/After drift chart — shows sync improvement over time.
 *
 * Gray dashed line = raw drift before correction.
 * Green solid line = residual drift after correction.
 * Filled area between = improvement.
 */
export function DriftChart({ data, isLoading }: DriftChartProps) {
  if (isLoading) {
    return (
      <div className="flex h-20 items-center justify-center text-[10px] text-muted">
        Loading drift data…
      </div>
    );
  }

  if (!data || data.frames.length === 0) {
    return (
      <div className="flex h-20 items-center justify-center text-[10px] text-muted">
        No drift data available
      </div>
    );
  }

  const { frames, beforeDrift, afterDrift, improvementPct } = data;
  const n = frames.length;

  // Compute bounds
  const allValues = [...beforeDrift, ...afterDrift].filter(
    (v) => !Number.isNaN(v),
  );
  const yMax = Math.max(...allValues, 1);
  const yMin = Math.min(...allValues, 0);
  const yRange = yMax - yMin || 1;

  const plotW = 100; // viewBox percentage
  const plotH = CHART_H - PADDING.top - PADDING.bottom;

  const xScale = (i: number) =>
    PADDING.left + ((plotW - PADDING.left - PADDING.right) * i) / Math.max(n - 1, 1);
  const yScale = (v: number) =>
    PADDING.top + plotH - ((v - yMin) / yRange) * plotH;

  // Build SVG paths
  const beforePath = beforeDrift
    .map((v, i) => `${i === 0 ? "M" : "L"}${xScale(i)},${yScale(v)}`)
    .join(" ");
  const afterPath = afterDrift
    .map((v, i) => `${i === 0 ? "M" : "L"}${xScale(i)},${yScale(v)}`)
    .join(" ");
  // Fill area between
  const fillPath =
    beforePath +
    " " +
    afterDrift
      .map((v, i) => `L${xScale(n - 1 - i)},${yScale(v)}`)
      .reverse()
      .join(" ") +
    " Z";

  return (
    <div className="px-3 py-2">
      {/* Header */}
      <div className="mb-1 flex items-center gap-4">
        <span className="text-[10px] font-medium uppercase tracking-wider text-muted">
          Before / After Drift
        </span>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1">
            <div className="h-px w-3 border-t border-dashed border-foreground/30" />
            <span className="text-[9px] text-muted">Before</span>
          </div>
          <div className="flex items-center gap-1">
            <div className="h-0.5 w-3 rounded-full bg-primary" />
            <span className="text-[9px] text-muted">After</span>
          </div>
        </div>
        {improvementPct > 0 && (
          <span className="ml-auto text-[10px] font-medium text-success">
            ↓ {improvementPct.toFixed(0)}% improved
          </span>
        )}
      </div>

      {/* Chart */}
      <svg
        viewBox={`0 0 ${plotW} ${CHART_H}`}
        className="h-20 w-full"
        preserveAspectRatio="none"
      >
        {/* Y-axis labels */}
        <text
          x={PADDING.left - 4}
          y={PADDING.top + 4}
          textAnchor="end"
          className="fill-muted"
          fontSize={5}
        >
          {yMax.toFixed(0)}ms
        </text>
        <text
          x={PADDING.left - 4}
          y={PADDING.top + plotH}
          textAnchor="end"
          className="fill-muted"
          fontSize={5}
        >
          {yMin.toFixed(0)}ms
        </text>

        {/* Zero line */}
        {yMin <= 0 && yMax >= 0 && (
          <line
            x1={PADDING.left}
            y1={yScale(0)}
            x2={plotW - PADDING.right}
            y2={yScale(0)}
            stroke="currentColor"
            strokeOpacity={0.1}
            strokeWidth={0.3}
          />
        )}

        {/* Fill between before and after */}
        <path d={fillPath} fill="currentColor" className="text-primary" fillOpacity={0.06} />

        {/* Before drift (gray dashed) */}
        <path
          d={beforePath}
          fill="none"
          stroke="currentColor"
          strokeOpacity={0.25}
          strokeWidth={0.8}
          strokeDasharray="2,2"
          vectorEffect="non-scaling-stroke"
        />

        {/* After drift (green solid) */}
        <path
          d={afterPath}
          fill="none"
          className="text-primary"
          stroke="currentColor"
          strokeWidth={1}
          vectorEffect="non-scaling-stroke"
        />
      </svg>
    </div>
  );
}
