import { useCallback, useRef, useState } from "react";

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
  const [isDragging, setIsDragging] = useState(false);
  const progress = duration > 0 ? (currentTime / duration) * 100 : 0;

  const seekFromEvent = useCallback(
    (clientX: number) => {
      const bar = barRef.current;
      if (!bar || duration <= 0) return;
      const rect = bar.getBoundingClientRect();
      const pct = Math.max(
        0,
        Math.min(1, (clientX - rect.left) / rect.width),
      );
      onSeek(pct * duration);
    },
    [duration, onSeek],
  );

  const handlePointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      setIsDragging(true);
      seekFromEvent(e.clientX);
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
    },
    [seekFromEvent],
  );

  const handlePointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!isDragging) return;
      seekFromEvent(e.clientX);
    },
    [isDragging, seekFromEvent],
  );

  const handlePointerUp = useCallback(() => {
    setIsDragging(false);
  }, []);

  return (
    <div className="flex h-10 items-center gap-3 border-t px-4">
      {/* Play/Pause */}
      <button
        onClick={onToggle}
        className="flex h-7 w-7 items-center justify-center rounded-md text-foreground transition-colors hover:bg-foreground/5 hover:text-primary"
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

      {/* Progress bar — click + drag to seek */}
      <div
        ref={barRef}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        className="group relative flex-1 cursor-pointer py-2"
      >
        <div className="h-1.5 rounded-full bg-foreground/10 transition-[height] group-hover:h-2">
          <div
            className="relative h-full rounded-full bg-primary"
            style={{ width: `${progress}%` }}
          >
            {/* Scrubber handle */}
            <div className="absolute -right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 rounded-full bg-primary opacity-0 shadow-sm transition-opacity group-hover:opacity-100" />
          </div>
        </div>
      </div>

      {/* Time display */}
      <span className="shrink-0 font-mono text-[11px] tabular-nums text-muted">
        {formatTime(currentTime)} / {formatTime(duration)}
      </span>

      {/* Playback rate */}
      <select
        value={playbackRate}
        onChange={(e) => onSetRate(Number(e.target.value))}
        className="rounded border bg-transparent px-1.5 py-0.5 text-[10px] text-muted"
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
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}
