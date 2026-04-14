import { useSensorReplay } from "@/hooks/use-sensor-replay";

interface ReviewSensorChartProps {
  episodeId: string;
  streamId: string;
  currentTime: number;
}

/**
 * Multi-channel line chart for a recorded sensor stream, with a
 * playback cursor drawn at ``currentTime``.
 *
 * Mirrors the visual language of the Record-mode :component:`SensorChart`
 * (thin strokes, OpenGraph Labs-ish neutral palette) but operates on
 * static episode data rather than a live SSE feed.
 */

const CHART_W = 228;
const CHART_H = 120;
const PADDING = { top: 8, right: 8, bottom: 20, left: 32 };

const SERIES_COLORS = [
  "#4F46E5", "#059669", "#D97706", "#DC2626", "#7C3AED", "#0891B2",
];

export function ReviewSensorChart({
  episodeId,
  streamId,
  currentTime,
}: ReviewSensorChartProps) {
  const { data, isLoading, error } = useSensorReplay(episodeId, streamId);

  if (isLoading) {
    return (
      <div className="flex h-full w-full items-center justify-center text-xs text-muted">
        Loading sensor…
      </div>
    );
  }
  if (error || !data || data.t.length < 2) {
    return (
      <div className="flex h-full w-full items-center justify-center text-xs text-muted">
        {error ?? "No sensor data"}
      </div>
    );
  }

  const { t, channels, duration_s } = data;
  const names = Object.keys(channels);

  // Global y range across all plotted channels.
  let yMin = Infinity;
  let yMax = -Infinity;
  for (const vs of Object.values(channels)) {
    for (const v of vs) {
      if (Number.isNaN(v)) continue;
      if (v < yMin) yMin = v;
      if (v > yMax) yMax = v;
    }
  }
  if (!isFinite(yMin) || !isFinite(yMax)) {
    yMin = 0;
    yMax = 1;
  }
  const yRange = yMax - yMin || 1;

  const plotW = CHART_W - PADDING.left - PADDING.right;
  const plotH = CHART_H - PADDING.top - PADDING.bottom;
  const xScale = (i: number) =>
    PADDING.left + (i / Math.max(t.length - 1, 1)) * plotW;
  const yScale = (v: number) =>
    PADDING.top + plotH - ((v - yMin) / yRange) * plotH;

  const cursorX =
    PADDING.left +
    Math.max(0, Math.min(1, currentTime / Math.max(duration_s, 1e-6))) * plotW;

  return (
    <div className="h-full w-full px-3 py-2">
      <svg
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        className="h-full w-full"
        preserveAspectRatio="none"
      >
        {/* Y-axis bounds */}
        <text
          x={PADDING.left - 4}
          y={PADDING.top + 4}
          textAnchor="end"
          className="fill-muted"
          fontSize={7}
        >
          {yMax.toFixed(1)}
        </text>
        <text
          x={PADDING.left - 4}
          y={PADDING.top + plotH}
          textAnchor="end"
          className="fill-muted"
          fontSize={7}
        >
          {yMin.toFixed(1)}
        </text>

        {/* Gridlines */}
        {[0, 0.5, 1].map((frac) => (
          <line
            key={frac}
            x1={PADDING.left}
            y1={PADDING.top + plotH * frac}
            x2={PADDING.left + plotW}
            y2={PADDING.top + plotH * frac}
            stroke="currentColor"
            strokeOpacity={0.08}
          />
        ))}

        {/* Channel polylines */}
        {names.map((name, ci) => {
          const values = channels[name]!;
          const points = values
            .map((v, i) =>
              Number.isNaN(v) ? null : `${xScale(i)},${yScale(v)}`,
            )
            .filter(Boolean)
            .join(" L ");
          if (!points) return null;
          return (
            <path
              key={name}
              d={`M ${points}`}
              fill="none"
              stroke={SERIES_COLORS[ci % SERIES_COLORS.length]}
              strokeWidth={1.2}
              strokeLinecap="round"
              strokeLinejoin="round"
              vectorEffect="non-scaling-stroke"
              opacity={0.9}
            />
          );
        })}

        {/* Playback cursor */}
        <line
          x1={cursorX}
          y1={PADDING.top}
          x2={cursorX}
          y2={PADDING.top + plotH}
          stroke="currentColor"
          strokeOpacity={0.55}
          strokeWidth={1}
        />

        {/* Legend */}
        {names.slice(0, 6).map((name, ci) => (
          <g key={name}>
            <rect
              x={PADDING.left + ci * 36}
              y={CHART_H - 12}
              width={6}
              height={6}
              rx={1}
              fill={SERIES_COLORS[ci % SERIES_COLORS.length]}
            />
            <text
              x={PADDING.left + ci * 36 + 9}
              y={CHART_H - 6}
              className="fill-muted"
              fontSize={6}
            >
              {name}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}
