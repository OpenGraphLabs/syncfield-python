import { useCallback, useRef } from "react";

interface ReviewTimelineProps {
  currentTime: number;
  duration: number;
  isPlaying: boolean;
  playbackRate: number;
  onSeek: (time: number) => void;
  onToggle: () => void;
  onSetRate: (rate: number) => void;
}

const RATES = [0.25, 0.5, 1, 1.5, 2];

export function ReviewTimeline({
  currentTime,
  duration,
  isPlaying,
  playbackRate,
  onSeek,
  onToggle,
  onSetRate,
}: ReviewTimelineProps) {
  const barRef = useRef<HTMLDivElement>(null);
  const progress = duration > 0 ? (currentTime / duration) * 100 : 0;

  const handleBarClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      const bar = barRef.current;
      if (!bar || duration <= 0) return;
      const rect = bar.getBoundingClientRect();
      const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      onSeek(pct * duration);
    },
    [duration, onSeek],
  );

  return (
    <div className="flex h-9 items-center gap-3 border-t px-4">
      {/* Play/Pause */}
      <button
        onClick={onToggle}
        className="text-foreground transition-colors hover:text-primary"
      >
        {isPlaying ? (
          <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
            <rect x="3" y="2" width="4" height="12" rx="1" />
            <rect x="9" y="2" width="4" height="12" rx="1" />
          </svg>
        ) : (
          <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
            <path d="M4 2.5v11l9-5.5z" />
          </svg>
        )}
      </button>

      {/* Progress bar */}
      <div
        ref={barRef}
        onClick={handleBarClick}
        className="flex-1 cursor-pointer py-2"
      >
        <div className="h-1 rounded-full bg-foreground/10">
          <div
            className="h-full rounded-full bg-primary transition-[width] duration-100"
            style={{ width: `${progress}%` }}
          />
        </div>
      </div>

      {/* Time display */}
      <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted">
        {formatTime(currentTime)} / {formatTime(duration)}
      </span>

      {/* Playback rate */}
      <select
        value={playbackRate}
        onChange={(e) => onSetRate(Number(e.target.value))}
        className="rounded border bg-transparent px-1 py-0.5 text-[10px] text-muted"
      >
        {RATES.map((r) => (
          <option key={r} value={r}>
            {r}x
          </option>
        ))}
      </select>
    </div>
  );
}

function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds)) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}
