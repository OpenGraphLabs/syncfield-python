import { useCallback, useEffect, useState } from "react";

interface ReviewAudioPanelProps {
  episodeId: string;
  streamId: string;
  /** Current playback time in seconds (from the primary ``<video>``). */
  currentTime: number;
}

interface WaveformData {
  sample_rate: number;
  duration_s: number;
  channels: number;
  envelope: [number, number][];
}

/**
 * Review-mode audio panel — fills a left-area stream slot with the
 * recorded waveform plus a playback cursor that slides with the
 * primary video's ``currentTime``. Replaces the tiny sidebar waveform
 * that used to be the only audio affordance in Review.
 */

const W = 228;
const H = 120;
const PAD = { left: 8, right: 8, top: 10, bottom: 22 };

export function ReviewAudioPanel({
  episodeId,
  streamId,
  currentTime,
}: ReviewAudioPanelProps) {
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
    return (
      <div className="flex h-full w-full items-center justify-center text-xs text-muted">
        Loading waveform…
      </div>
    );
  }
  if (!data || data.envelope.length === 0) {
    return (
      <div className="flex h-full w-full items-center justify-center text-xs text-muted">
        No audio data
      </div>
    );
  }

  const { envelope, duration_s, sample_rate } = data;
  const n = envelope.length;
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  const midY = PAD.top + plotH / 2;
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

  const cursorX =
    PAD.left +
    Math.max(0, Math.min(1, currentTime / Math.max(duration_s, 1e-6))) * plotW;

  return (
    <div className="h-full w-full px-3 py-2">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="h-full w-full"
        preserveAspectRatio="none"
      >
        {/* Midline */}
        <line
          x1={PAD.left}
          y1={midY}
          x2={W - PAD.right}
          y2={midY}
          stroke="currentColor"
          strokeOpacity={0.08}
        />

        {/* Waveform envelope — symmetric upper/lower strokes. A fill
            path would double the DOM size for the same visual, so we
            lean on two thin lines instead. */}
        <path
          d={upperPath}
          fill="none"
          className="text-primary"
          stroke="currentColor"
          strokeWidth={0.8}
          vectorEffect="non-scaling-stroke"
        />
        <path
          d={lowerPath}
          fill="none"
          className="text-primary"
          stroke="currentColor"
          strokeWidth={0.8}
          vectorEffect="non-scaling-stroke"
        />

        {/* Playback cursor */}
        <line
          x1={cursorX}
          y1={PAD.top}
          x2={cursorX}
          y2={PAD.top + plotH}
          stroke="currentColor"
          strokeOpacity={0.55}
          strokeWidth={1}
        />

        {/* Time labels + meta */}
        <text
          x={PAD.left}
          y={H - 6}
          className="fill-muted"
          fontSize={7}
        >
          0s
        </text>
        <text
          x={W - PAD.right}
          y={H - 6}
          textAnchor="end"
          className="fill-muted"
          fontSize={7}
        >
          {duration_s.toFixed(1)}s
        </text>
        <text
          x={W / 2}
          y={H - 6}
          textAnchor="middle"
          className="fill-muted font-mono"
          fontSize={7}
        >
          {(sample_rate / 1000).toFixed(1)} kHz
        </text>
      </svg>
    </div>
  );
}
