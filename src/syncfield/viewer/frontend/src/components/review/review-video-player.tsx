import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";

interface ReviewVideoPlayerProps {
  episodeId: string;
  streamId: string;
  isPrimary: boolean;
  videoRef?: (el: HTMLVideoElement | null) => void;
  syncTime?: number;
  driftMs?: number;
  /** Click handler for opening sync comparison (secondary streams only). */
  onClick?: () => void;
}

export function ReviewVideoPlayer({
  episodeId,
  streamId,
  isPrimary,
  videoRef,
  syncTime,
  driftMs,
  onClick,
}: ReviewVideoPlayerProps) {
  const localRef = useRef<HTMLVideoElement | null>(null);

  // Sync secondary videos to primary's currentTime
  useEffect(() => {
    if (isPrimary || syncTime == null) return;
    const video = localRef.current;
    if (!video) return;
    if (Math.abs(video.currentTime - syncTime) > 0.1) {
      video.currentTime = syncTime;
    }
  }, [syncTime, isPrimary]);

  // Show first frame on load
  useEffect(() => {
    const video = localRef.current;
    if (!video) return;
    const showFirstFrame = () => {
      if (video.paused && video.currentTime === 0) {
        video.currentTime = 0.001;
      }
    };
    video.addEventListener("loadeddata", showFirstFrame);
    return () => video.removeEventListener("loadeddata", showFirstFrame);
  }, []);

  const src = `/api/episodes/${episodeId}/video/${streamId}.mp4`;
  const isClickable = !isPrimary && onClick != null;

  return (
    <div
      className={cn(
        "relative flex-1 overflow-hidden rounded-lg bg-black",
        isClickable && "cursor-pointer ring-transparent transition-all hover:ring-2 hover:ring-primary/40",
      )}
      onClick={isClickable ? onClick : undefined}
    >
      <video
        ref={(el) => {
          localRef.current = el;
          if (isPrimary && videoRef) videoRef(el);
        }}
        src={src}
        className="h-full w-full object-contain"
        muted
        playsInline
        preload="auto"
      />
      {/* Stream label */}
      <div className="absolute left-2 top-2 rounded bg-black/60 px-1.5 py-0.5 text-[10px] font-medium text-white/80">
        {streamId}
        {isPrimary && (
          <span className="ml-1 text-white/40">REF</span>
        )}
      </div>
      {/* Drift badge + click hint */}
      {!isPrimary && driftMs != null && (
        <div className="absolute bottom-2 right-2 flex items-center gap-1.5">
          {isClickable && (
            <span className="rounded bg-black/40 px-1 py-0.5 text-[9px] text-white/50">
              Click to compare
            </span>
          )}
          <span className={cn(
            "rounded bg-black/60 px-1.5 py-0.5 font-mono text-[10px]",
            Math.abs(driftMs) < 5 ? "text-success" :
            Math.abs(driftMs) < 20 ? "text-warning" :
            "text-destructive",
          )}>
            {driftMs > 0 ? "+" : ""}{driftMs.toFixed(1)}ms
          </span>
        </div>
      )}
    </div>
  );
}
