import { useSensorStream } from "@/hooks/use-sensor-stream";

interface SensorChartProps {
  streamId: string;
}

/**
 * Real-time sensor chart rendered as inline SVG.
 *
 * Connects to the SSE endpoint via `useSensorStream`, maintains a
 * rolling buffer of 300 points per channel, and draws each channel
 * as a polyline on a shared coordinate system. No chart library
 * dependency — just raw SVG paths.
 */

const CHART_W = 228;
const CHART_H = 120;
const PADDING = { top: 8, right: 8, bottom: 16, left: 32 };

const SERIES_COLORS = [
  "#4F46E5", // indigo
  "#059669", // emerald
  "#D97706", // amber
  "#DC2626", // red
  "#7C3AED", // violet
  "#0891B2", // cyan
];

export function SensorChart({ streamId }: SensorChartProps) {
  const { channels, labels, isConnected } = useSensorStream(streamId);
  const channelNames = Object.keys(channels);

  if (channelNames.length === 0) {
    return (
      <div className="flex h-[146px] items-center justify-center text-xs text-muted">
        {isConnected ? "Waiting for data…" : "Connecting…"}
      </div>
    );
  }

  // Compute axis bounds from all channels
  const allValues = channelNames.flatMap((name) => channels[name] ?? []);
  const yMin = Math.min(...allValues);
  const yMax = Math.max(...allValues);
  const yRange = yMax - yMin || 1; // Avoid division by zero

  const plotW = CHART_W - PADDING.left - PADDING.right;
  const plotH = CHART_H - PADDING.top - PADDING.bottom;

  const xScale = (i: number) =>
    PADDING.left + (i / Math.max(labels.length - 1, 1)) * plotW;
  const yScale = (v: number) =>
    PADDING.top + plotH - ((v - yMin) / yRange) * plotH;

  return (
    <div className="h-[146px] px-2 py-1">
      <svg
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        className="h-full w-full"
        preserveAspectRatio="none"
      >
        {/* Y-axis labels */}
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

        {/* Grid lines */}
        <line
          x1={PADDING.left}
          y1={PADDING.top}
          x2={PADDING.left + plotW}
          y2={PADDING.top}
          stroke="currentColor"
          strokeOpacity={0.08}
        />
        <line
          x1={PADDING.left}
          y1={PADDING.top + plotH / 2}
          x2={PADDING.left + plotW}
          y2={PADDING.top + plotH / 2}
          stroke="currentColor"
          strokeOpacity={0.08}
        />
        <line
          x1={PADDING.left}
          y1={PADDING.top + plotH}
          x2={PADDING.left + plotW}
          y2={PADDING.top + plotH}
          stroke="currentColor"
          strokeOpacity={0.08}
        />

        {/* Data lines */}
        {channelNames.map((name, ci) => {
          const values = channels[name];
          if (!values || values.length < 2) return null;

          const points = values
            .map((v, i) => {
              if (Number.isNaN(v)) return null;
              return `${xScale(i)},${yScale(v)}`;
            })
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
            />
          );
        })}

        {/* Channel legend */}
        {channelNames.slice(0, 6).map((name, ci) => (
          <g key={name}>
            <rect
              x={PADDING.left + ci * 36}
              y={CHART_H - 10}
              width={6}
              height={6}
              rx={1}
              fill={SERIES_COLORS[ci % SERIES_COLORS.length]}
            />
            <text
              x={PADDING.left + ci * 36 + 9}
              y={CHART_H - 4}
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
