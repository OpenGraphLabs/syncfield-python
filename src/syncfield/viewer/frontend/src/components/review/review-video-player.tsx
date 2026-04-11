import { useEffect, useRef } from "react";

interface ReviewVideoPlayerProps {
  episodeId: string;
  streamId: string;
  isPrimary: boolean;
  /** Ref callback for the primary video — attaches playback control. */
  videoRef?: (el: HTMLVideoElement | null) => void;
  /** Current time to sync secondary videos to. */
  syncTime?: number;
  /** Drift offset in ms (shown as badge on non-primary). */
  driftMs?: number;
}

/**
 * Video player for episode review. Renders the recorded video from
 * the server's episode video endpoint, preferring synced/ versions.
 */
export function ReviewVideoPlayer({
  episodeId,
  streamId,
  isPrimary,
  videoRef,
  syncTime,
  driftMs,
}: ReviewVideoPlayerProps) {
  const localRef = useRef<HTMLVideoElement | null>(null);

  // Sync secondary videos to primary's currentTime
  useEffect(() => {
    if (isPrimary || syncTime == null) return;
    const video = localRef.current;
    if (!video) return;
    // Only sync if the difference is significant (> 100ms)
    if (Math.abs(video.currentTime - syncTime) > 0.1) {
      video.currentTime = syncTime;
    }
  }, [syncTime, isPrimary]);

  const src = `/api/episodes/${episodeId}/video/${streamId}.mp4`;

  return (
    <div className="relative flex-1 overflow-hidden rounded-lg bg-black">
      <video
        ref={(el) => {
          localRef.current = el;
          if (isPrimary && videoRef) videoRef(el);
        }}
        src={src}
        className="h-full w-full object-contain"
        muted
        playsInline
        preload="metadata"
      />
      {/* Stream label */}
      <div className="absolute left-2 top-2 rounded bg-black/60 px-1.5 py-0.5 text-[10px] font-medium text-white/80">
        {streamId}
        {isPrimary && (
          <span className="ml-1 text-primary-foreground/60">REF</span>
        )}
      </div>
      {/* Drift badge (non-primary only) */}
      {!isPrimary && driftMs != null && (
        <div className="absolute bottom-2 right-2 rounded bg-black/60 px-1.5 py-0.5 font-mono text-[10px] text-success">
          {driftMs > 0 ? "+" : ""}
          {driftMs.toFixed(1)}ms
        </div>
      )}
    </div>
  );
}
