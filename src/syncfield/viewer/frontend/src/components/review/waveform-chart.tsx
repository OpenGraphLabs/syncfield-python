import { useCallback, useEffect, useState } from "react";

interface WaveformChartProps {
  episodeId: string;
  streamId: string;
}

interface WaveformData {
  sample_rate: number;
  duration_s: number;
  channels: number;
  envelope: [number, number][];
}

/**
 * Audio waveform visualization for Review mode.
 *
 * Fetches a downsampled min/max envelope from the server and renders
 * it as a mirrored SVG waveform. Shows duration and sample rate info.
 */
export function WaveformChart({ episodeId, streamId }: WaveformChartProps) {
  const [data, setData] = useState<WaveformData | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const fetchWaveform = useCallback(async () => {
    setIsLoading(true);
    try {
      const res = await fetch(
        `/api/episodes/${episodeId}/waveform/${streamId}`,
      );
      if (!res.ok) {
        setData(null);
        return;
      }
      const json = await res.json();
      if (json.envelope) setData(json);
    } catch {
      setData(null);
    } finally {
      setIsLoading(false);
    }
  }, [episodeId, streamId]);

  useEffect(() => {
    void fetchWaveform();
  }, [fetchWaveform]);

  if (isLoading) {
    return <Placeholder>Loading waveform…</Placeholder>;
  }

  if (!data || data.envelope.length === 0) {
    return <Placeholder>No audio data</Placeholder>;
  }

  const { envelope, duration_s, sample_rate } = data;
  const n = envelope.length;

  const W = 800;
  const H = 80;
  const PAD = { left: 4, right: 4, top: 4, bottom: 14 };
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  const midY = PAD.top + plotH / 2;

  // Build upper and lower envelope paths
  const step = plotW / Math.max(n - 1, 1);

  let upperPath = "";
  let lowerPath = "";
  for (let i = 0; i < n; i++) {
    const x = PAD.left + i * step;
    const [lo, hi] = envelope[i]!;
    const yHi = midY - hi * (plotH / 2);
    const yLo = midY - lo * (plotH / 2);
    upperPath += `${i === 0 ? "M" : "L"}${x},${yHi}`;
    lowerPath += `${i === 0 ? "M" : "L"}${x},${yLo}`;
  }

  // Close the fill area
  const fillPath =
    upperPath +
    `L${PAD.left + (n - 1) * step},${midY}` +
    lowerPath
      .split(/[ML]/)
      .filter(Boolean)
      .reverse()
      .map((p, i) => `${i === 0 ? "L" : "L"}${p}`)
      .join("") +
    `L${PAD.left},${midY}Z`;

  return (
    <div className="px-3 py-2">
      <div className="mb-1 flex items-center gap-3 text-[10px]">
        <span className="font-medium uppercase tracking-wider text-muted">
          Audio Waveform
        </span>
        <span className="text-muted">
          {duration_s.toFixed(1)}s · {(sample_rate / 1000).toFixed(1)} kHz
        </span>
      </div>

      <svg viewBox={`0 0 ${W} ${H}`} className="h-20 w-full">
        {/* Center line */}
        <line
          x1={PAD.left}
          y1={midY}
          x2={W - PAD.right}
          y2={midY}
          stroke="currentColor"
          strokeOpacity={0.08}
        />

        {/* Waveform fill */}
        <path d={fillPath} fill="currentColor" className="text-primary" fillOpacity={0.15} />

        {/* Upper envelope */}
        <path d={upperPath} fill="none" className="text-primary" stroke="currentColor" strokeWidth={0.8} />

        {/* Lower envelope */}
        <path d={lowerPath} fill="none" className="text-primary" stroke="currentColor" strokeWidth={0.8} />

        {/* Time labels */}
        <text x={PAD.left} y={H - 2} fill="currentColor" opacity={0.3} fontSize={7}>
          0s
        </text>
        <text x={W - PAD.right} y={H - 2} textAnchor="end" fill="currentColor" opacity={0.3} fontSize={7}>
          {duration_s.toFixed(0)}s
        </text>
      </svg>
    </div>
  );
}

function Placeholder({ children }: { children: string }) {
  return (
    <div className="flex h-20 items-center justify-center text-[10px] text-muted">
      {children}
    </div>
  );
}
