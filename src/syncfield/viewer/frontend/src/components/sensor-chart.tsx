import { useSensorStream } from "@/hooks/use-sensor-stream";

interface SensorChartProps {
  streamId: string;
  windowSeconds?: number;
  variant?: "aspect" | "fill";
}

/**
 * Real-time sensor chart rendered as inline SVG.
 *
 * Connects to the SSE endpoint via `useSensorStream`, maintains a
 * rolling buffer sized to the visible window, and draws each channel
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

export function SensorChart({
  streamId,
  windowSeconds = 5,
  variant = "aspect",
}: SensorChartProps) {
  const maxPoints = Math.min(6000, Math.max(300, Math.round(windowSeconds * 200)));
  const { channels, labels, isConnected } = useSensorStream(streamId, { maxPoints });
  const channelNames = Object.keys(channels);
  const wrapperClass =
    variant === "fill" ? "h-full w-full px-3 py-2" : "aspect-video px-3 py-2";
  const placeholderClass =
    variant === "fill"
      ? "flex h-full w-full items-center justify-center text-xs text-muted"
      : "flex aspect-video items-center justify-center text-xs text-muted";

  if (channelNames.length === 0) {
    return (
      <div className={placeholderClass}>
        {isConnected ? "Waiting for data…" : "Connecting…"}
      </div>
    );
  }

  const windowed = windowChannels(channels, labels, windowSeconds);
  const visibleChannels = windowed.channels;

  // Compute axis bounds from visible channels
  const allValues = channelNames.flatMap((name) => visibleChannels[name] ?? []);
  const yMin = Math.min(...allValues);
  const yMax = Math.max(...allValues);
  const yRange = yMax - yMin || 1; // Avoid division by zero

  const plotW = CHART_W - PADDING.left - PADDING.right;
  const plotH = CHART_H - PADDING.top - PADDING.bottom;

  const yScale = (v: number) =>
    PADDING.top + plotH - ((v - yMin) / yRange) * plotH;

  return (
    <div className={wrapperClass}>
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
          const values = visibleChannels[name];
          if (!values || values.length < 2) return null;

          const points = values
            .map((v, i) => {
              if (Number.isNaN(v)) return null;
              const x = PADDING.left + (i / Math.max(values.length - 1, 1)) * plotW;
              return `${x},${yScale(v)}`;
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
        {channelNames.slice(0, 5).map((name, ci) => (
          <g key={name}>
            <rect
              x={PADDING.left + ci * 34}
              y={CHART_H - 10}
              width={6}
              height={6}
              rx={1}
              fill={SERIES_COLORS[ci % SERIES_COLORS.length]}
            />
            <text
              x={PADDING.left + ci * 34 + 9}
              y={CHART_H - 4}
              className="fill-muted"
              fontSize={6}
            >
              {name}
            </text>
          </g>
        ))}
        <text
          x={CHART_W - PADDING.right}
          y={CHART_H - 4}
          textAnchor="end"
          className="fill-muted font-mono"
          fontSize={6}
        >
          {windowSeconds}s
        </text>
      </svg>
    </div>
  );
}

function windowChannels(
  channels: Record<string, number[]>,
  labels: number[],
  windowSeconds: number,
): { channels: Record<string, number[]>; labels: number[] } {
  const longestSeries = Math.max(0, ...Object.values(channels).map((values) => values.length));
  if (longestSeries === 0) return { channels, labels };

  const labelStart = labelWindowStart(labels, windowSeconds);
  const fallbackCount = Math.min(
    longestSeries,
    Math.max(60, Math.round(windowSeconds * 200)),
  );
  const start = labelStart ?? Math.max(0, longestSeries - fallbackCount);
  const nextChannels = Object.fromEntries(
    Object.entries(channels).map(([name, values]) => [
      name,
      values.slice(Math.max(0, values.length - (longestSeries - start))),
    ]),
  );
  const nextLabels =
    labels.length > 0
      ? labels.slice(Math.max(0, labels.length - (longestSeries - start)))
      : Array.from({ length: longestSeries - start }, (_, i) => i);
  return { channels: nextChannels, labels: nextLabels };
}

function labelWindowStart(labels: number[], windowSeconds: number): number | null {
  if (labels.length < 2) return null;
  const latest = labels[labels.length - 1];
  const first = labels[0];
  if (latest === undefined || first === undefined || latest <= first) return null;
  const span = latest - first;
  const unitsPerSecond = span > 1_000_000 ? 1_000_000_000 : span > 1_000 ? 1_000 : 1;
  const threshold = latest - windowSeconds * unitsPerSecond;
  const index = labels.findIndex((label) => label >= threshold);
  return index >= 0 ? index : null;
}
