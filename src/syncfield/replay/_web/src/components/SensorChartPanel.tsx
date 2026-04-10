import { useMemo } from "react";
import type { SensorStreamData } from "../hooks/useSensorData";

interface Props {
  sensors: SensorStreamData[];
  masterTime: number;     // seconds
  duration: number;       // seconds
}

const CHART_W = 240;
const CHART_H = 56;
const PAD_X = 4;
const PAD_Y = 6;

interface ChannelPath {
  name: string;
  path: string;
  min: number;
  max: number;
}

function buildChannelPath(
  samples: SensorStreamData["samples"],
  channel: string,
): ChannelPath {
  if (samples.length === 0) return { name: channel, path: "", min: 0, max: 0 };
  const t0 = samples[0].t_ns;
  const tEnd = samples[samples.length - 1].t_ns;
  const span = Math.max(1, tEnd - t0);

  let min = Infinity;
  let max = -Infinity;
  for (const s of samples) {
    const v = s.channels[channel];
    if (typeof v !== "number") continue;
    if (v < min) min = v;
    if (v > max) max = v;
  }
  if (min === Infinity) return { name: channel, path: "", min: 0, max: 0 };
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const yRange = max - min;

  let d = "";
  for (let i = 0; i < samples.length; i++) {
    const s = samples[i];
    const v = s.channels[channel];
    if (typeof v !== "number") continue;
    const x = PAD_X + ((s.t_ns - t0) / span) * (CHART_W - 2 * PAD_X);
    const y = PAD_Y + (1 - (v - min) / yRange) * (CHART_H - 2 * PAD_Y);
    d += i === 0 ? `M${x.toFixed(1)},${y.toFixed(1)}` : `L${x.toFixed(1)},${y.toFixed(1)}`;
  }
  return { name: channel, path: d, min, max };
}

function StreamGroup({
  stream,
  playheadX,
}: {
  stream: SensorStreamData;
  playheadX: number;
}) {
  const channelPaths = useMemo(
    () => stream.channelNames.map((c) => buildChannelPath(stream.samples, c)),
    [stream],
  );

  return (
    <div className="mb-4">
      <div className="mb-1 flex items-center justify-between">
        <span className="font-mono text-[11px] text-zinc-600">{stream.id}</span>
        <span className="text-[10px] text-zinc-400">
          {stream.samples.length} samples
        </span>
      </div>
      <div className="space-y-1">
        {channelPaths.map((cp) => (
          <div key={cp.name} className="flex items-center gap-2">
            <span className="w-10 text-right font-mono text-[10px] text-zinc-400">
              {cp.name}
            </span>
            <svg
              className="sensor-chart"
              width={CHART_W}
              height={CHART_H}
              viewBox={`0 0 ${CHART_W} ${CHART_H}`}
            >
              <path
                d={cp.path}
                fill="none"
                stroke="#0891b2"
                strokeWidth={1}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
              <line
                x1={playheadX}
                x2={playheadX}
                y1={0}
                y2={CHART_H}
                stroke="#a1a1aa"
                strokeWidth={0.5}
                strokeDasharray="2,2"
              />
            </svg>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function SensorChartPanel({
  sensors,
  masterTime,
  duration,
}: Props) {
  if (sensors.length === 0) {
    return (
      <div className="p-4 text-xs text-zinc-400">
        No sensor streams in this session
      </div>
    );
  }

  const playheadX =
    duration > 0
      ? PAD_X + (masterTime / duration) * (CHART_W - 2 * PAD_X)
      : PAD_X;

  return (
    <div className="h-full overflow-y-auto p-3">
      {sensors.map((s) => (
        <StreamGroup key={s.id} stream={s} playheadX={playheadX} />
      ))}
    </div>
  );
}
