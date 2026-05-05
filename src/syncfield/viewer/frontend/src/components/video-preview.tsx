import { useState } from "react";
import { cn } from "@/lib/utils";

interface VideoPreviewProps {
  streamId: string;
  variant?: "aspect" | "fill";
}

/**
 * MJPEG video preview — renders as a plain `<img>` tag pointed at
 * the server's MJPEG endpoint. The browser handles frame decoding
 * natively with zero JavaScript overhead.
 *
 * While the first frame is in-flight we overlay a "waiting" state so
 * the card isn't just a blank box during adapter warmup.
 */
export function VideoPreview({ streamId, variant = "aspect" }: VideoPreviewProps) {
  const [loaded, setLoaded] = useState(false);
  const sizing = variant === "fill" ? "h-full w-full" : "aspect-video w-full";
  return (
    <div className={cn("relative overflow-hidden bg-background-subtle", sizing)}>
      <img
        src={`/stream/video/${streamId}`}
        alt={`${streamId} preview`}
        className={cn(
          "h-full w-full object-contain transition-opacity duration-150",
          loaded ? "opacity-100" : "opacity-0",
        )}
        onLoad={() => setLoaded(true)}
      />
      {!loaded && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <div className="flex items-center gap-2 text-xs text-muted">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-warning" />
            Waiting for first frame…
          </div>
        </div>
      )}
    </div>
  );
}
