import { useEffect, useMemo, useRef } from "react";
import { cn } from "@/lib/utils";

interface ReviewVideoPlayerProps {
  episodeId: string;
  streamId: string;
  isPrimary: boolean;
  videoRef?: (el: HTMLVideoElement | null) => void;
  syncTime?: number;
  /** Whether the primary video is playing — used to sync secondary play state. */
  isPlaying?: boolean;
  driftMs?: number;
  onClick?: () => void;
}

export function ReviewVideoPlayer({
  episodeId,
  streamId,
  isPrimary,
  videoRef,
  syncTime,
  isPlaying,
  driftMs,
  onClick,
}: ReviewVideoPlayerProps) {
  const localRef = useRef<HTMLVideoElement | null>(null);

  // Sync secondary videos to primary — debounced to avoid stutter
  const lastSyncRef = useRef(0);
  useEffect(() => {
    if (isPrimary || syncTime == null) return;
    const video = localRef.current;
    if (!video) return;
    // Only sync if drift exceeds 300ms (avoids constant seeking during playback)
    const drift = Math.abs(video.currentTime - syncTime);
    const now = performance.now();
    if (drift > 0.3 && now - lastSyncRef.current > 500) {
      video.currentTime = syncTime;
      lastSyncRef.current = now;
    }
  }, [syncTime, isPrimary]);

  // Sync play/pause state with primary
  useEffect(() => {
    if (isPrimary || isPlaying == null) return;
    const video = localRef.current;
    if (!video) return;
    if (isPlaying && video.paused) {
      void video.play();
    } else if (!isPlaying && !video.paused) {
      video.pause();
    }
  }, [isPlaying, isPrimary]);

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

  // Cache-bust: stable v= per component mount so the browser fetches
  // fresh content after re-mount (e.g. after sync completes).
  const mountToken = useMemo(() => Date.now(), []);
  const src = `/api/episodes/${episodeId}/video/${streamId}.mp4?v=${mountToken}`;
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
      <div className="absolute left-2 top-2 flex items-center gap-1.5">
        <span className="rounded bg-black/60 px-1.5 py-0.5 text-[10px] font-medium text-white/80">
          {streamId}
        </span>
        {isPrimary && (
          <span className="rounded bg-blue-500/80 px-1.5 py-0.5 text-[9px] font-semibold text-white">
            Primary
          </span>
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
