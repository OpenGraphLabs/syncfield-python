import { useEffect, useRef, useState } from "react";
import { useSensorStream } from "@/hooks/use-sensor-stream";

interface AudioLevelChartProps {
  streamId: string;
}

/**
 * Real-time audio level meter for Recording mode.
 *
 * Connects via SSE to the host_audio stream's RMS/peak channels
 * and renders a horizontal VU-meter bar with waveform history.
 * When no new data arrives for 1+ second, shows a faded idle state.
 */
export function AudioLevelChart({ streamId }: AudioLevelChartProps) {
  const { channels, isConnected } = useSensorStream(streamId);
  const [isActive, setIsActive] = useState(false);
  const prevLenRef = useRef(0);
  const idleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const rmsValues = channels["rms"] ?? [];
  const peakValues = channels["peak"] ?? [];
  const rms = rmsValues.length > 0 ? rmsValues[rmsValues.length - 1]! : 0;
  const peak = peakValues.length > 0 ? peakValues[peakValues.length - 1]! : 0;

  // Detect whether new data is arriving
  useEffect(() => {
    if (rmsValues.length !== prevLenRef.current) {
      prevLenRef.current = rmsValues.length;
      setIsActive(true);
      if (idleTimerRef.current) clearTimeout(idleTimerRef.current);
      idleTimerRef.current = setTimeout(() => setIsActive(false), 1500);
    }
    return () => {
      if (idleTimerRef.current) clearTimeout(idleTimerRef.current);
    };
  }, [rmsValues.length]);

  const rmsPercent = Math.min(100, rms * 200);
  const peakPercent = Math.min(100, peak * 100);

  if (!isConnected) {
    return (
      <div className="flex aspect-video items-center justify-center text-xs text-muted">
        Connecting…
      </div>
    );
  }

  // Idle state — mic detected but not recording
  if (!isActive) {
    return (
      <div className="flex aspect-video flex-col items-center justify-center gap-2 px-4 text-muted">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
          <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
          <line x1="12" y1="19" x2="12" y2="22" />
        </svg>
        <span className="text-[10px]">Microphone ready</span>
      </div>
    );
  }

  return (
    <div className="flex aspect-video flex-col items-center justify-center gap-3 px-4">
      {/* Waveform mini-history */}
      <div className="flex h-16 w-full items-end gap-px">
        {rmsValues.slice(-60).map((v, i) => {
          const h = Math.min(100, (v ?? 0) * 200);
          return (
            <div
              key={i}
              className="flex-1 rounded-t-sm"
              style={{
                height: `${Math.max(2, h)}%`,
                background:
                  h > 80
                    ? "hsl(0 65% 48%)"
                    : h > 50
                      ? "hsl(45 93% 47%)"
                      : "hsl(153 35% 38%)",
              }}
            />
          );
        })}
      </div>

      {/* Level bar */}
      <div className="w-full">
        <div className="relative h-2 w-full overflow-hidden rounded-full bg-foreground/10">
          <div
            className="absolute inset-y-0 left-0 rounded-full transition-[width] duration-100"
            style={{
              width: `${rmsPercent}%`,
              background:
                rmsPercent > 80
                  ? "hsl(0 65% 48%)"
                  : rmsPercent > 50
                    ? "hsl(45 93% 47%)"
                    : "hsl(153 35% 38%)",
            }}
          />
          <div
            className="absolute top-0 h-full w-0.5 bg-foreground/40"
            style={{ left: `${peakPercent}%` }}
          />
        </div>
        <div className="mt-1 flex justify-between text-[9px] text-muted">
          <span>RMS: {(rms * 100).toFixed(0)}%</span>
          <span>Peak: {(peak * 100).toFixed(0)}%</span>
        </div>
      </div>
    </div>
  );
}
