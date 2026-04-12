import { useCallback, useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

interface SyncComparisonModalProps {
  streamId: string;
  primaryStreamId: string;
  episodeId: string;
  driftMs: number;
  fps: number;
  currentTime: number;
  onClose: () => void;
}

/**
 * Sync before/after comparison modal with primary stream reference.
 *
 * Top: secondary stream with Before/After slider comparison.
 * Bottom: primary (reference) stream at the same timestamp.
 */
export function SyncComparisonModal({
  streamId,
  primaryStreamId,
  episodeId,
  driftMs,
  fps,
  currentTime,
  onClose,
}: SyncComparisonModalProps) {
  const [sliderPct, setSliderPct] = useState(50);
  const [isDragging, setIsDragging] = useState(false);
  const [showHint, setShowHint] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);

  const beforeCanvasRef = useRef<HTMLCanvasElement>(null);
  const afterCanvasRef = useRef<HTMLCanvasElement>(null);
  const primaryCanvasRef = useRef<HTMLCanvasElement>(null);
  const [framesCaptured, setFramesCaptured] = useState(false);

  const driftSec = driftMs / 1000;
  const correctedTime = currentTime;
  const uncorrectedTime = currentTime + driftSec;

  // Capture frames from both secondary and primary videos
  useEffect(() => {
    const secondarySrc = `/api/episodes/${episodeId}/video/${streamId}.mp4`;
    const primarySrc = `/api/episodes/${episodeId}/video/${primaryStreamId}.mp4`;

    Promise.all([
      captureFrames(secondarySrc, correctedTime, uncorrectedTime),
      captureOneFrame(primarySrc, correctedTime),
    ]).then(([secondary, primary]) => {
      drawToCanvas(afterCanvasRef.current, secondary.corrected, secondary.width, secondary.height);
      drawToCanvas(beforeCanvasRef.current, secondary.uncorrected, secondary.width, secondary.height);
      drawToCanvas(primaryCanvasRef.current, primary.frame, primary.width, primary.height);
      setFramesCaptured(true);
    }).catch(() => {});
  }, [episodeId, streamId, primaryStreamId, correctedTime, uncorrectedTime]);

  const handlePointerDown = useCallback((e: React.PointerEvent) => {
    setIsDragging(true);
    setShowHint(false);
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    updateSlider(e.clientX);
  }, []);

  const handlePointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (!isDragging) return;
      updateSlider(e.clientX);
    },
    [isDragging],
  );

  const handlePointerUp = useCallback(() => {
    setIsDragging(false);
  }, []);

  function updateSlider(clientX: number) {
    const container = containerRef.current;
    if (!container) return;
    const rect = container.getBoundingClientRect();
    const pct = ((clientX - rect.left) / rect.width) * 100;
    setSliderPct(Math.max(0, Math.min(100, pct)));
  }

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const absMs = Math.abs(driftMs);
  const sign = driftMs >= 0 ? "+" : "−";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={onClose} />

      <div className="relative w-full max-w-5xl rounded-2xl border bg-card shadow-2xl">
        {/* Header */}
        <div className="flex items-center gap-3 border-b px-5 py-3">
          <h2 className="text-sm font-semibold">Sync Comparison</h2>
          <span className="font-mono text-xs text-muted">{streamId}</span>
          <span className={cn(
            "rounded-md px-2 py-0.5 text-[10px] font-medium",
            absMs < 5 ? "bg-success/10 text-success" :
            absMs < 20 ? "bg-warning/10 text-warning" :
            "bg-destructive/10 text-destructive",
          )}>
            {sign}{absMs.toFixed(1)} ms
          </span>
          <div className="flex-1" />
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-muted transition-colors hover:bg-foreground/5 hover:text-foreground"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M4 4L12 12M12 4L4 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        {/* Two-row layout: secondary comparison + primary reference */}
        <div className="flex gap-px bg-black">
          {/* Secondary stream: Before/After slider */}
          <div className="flex-1">
            <div className="px-3 py-1.5 text-[10px] font-medium text-white/50">
              {streamId} — Before / After
            </div>
            <div
              ref={containerRef}
              className="relative aspect-video w-full cursor-ew-resize overflow-hidden"
              onPointerDown={handlePointerDown}
              onPointerMove={handlePointerMove}
              onPointerUp={handlePointerUp}
            >
              {/* After (corrected) — full background */}
              <canvas ref={afterCanvasRef} className="absolute inset-0 h-full w-full object-contain" />

              {/* Before (uncorrected) — clipped */}
              <div className="absolute inset-0" style={{ clipPath: `inset(0 ${100 - sliderPct}% 0 0)` }}>
                <canvas ref={beforeCanvasRef} className="h-full w-full object-contain" />
              </div>

              {/* Slider line + handle */}
              <div className="absolute top-0 bottom-0 w-0.5 bg-white/80" style={{ left: `${sliderPct}%` }}>
                <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-white p-1.5 shadow-md">
                  <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
                    <path d="M5 3L2 8L5 13" stroke="#333" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                    <path d="M11 3L14 8L11 13" stroke="#333" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </div>
              </div>

              {/* Labels */}
              <div
                className="absolute left-3 top-3 rounded-md bg-amber-500/80 px-2 py-0.5 text-[11px] font-semibold text-white"
                style={{ opacity: sliderPct > 15 ? 1 : 0 }}
              >
                Before
              </div>
              <div
                className="absolute right-3 top-3 rounded-md bg-blue-500/80 px-2 py-0.5 text-[11px] font-semibold text-white"
                style={{ opacity: sliderPct < 85 ? 1 : 0 }}
              >
                After
              </div>

              {showHint && framesCaptured && (
                <div className="absolute inset-x-0 bottom-3 flex justify-center">
                  <span className="rounded-full bg-black/50 px-3 py-1 text-[10px] text-white/70">
                    ← Drag to compare →
                  </span>
                </div>
              )}

              {!framesCaptured && (
                <div className="absolute inset-0 flex items-center justify-center">
                  <span className="text-xs text-white/40">Capturing frames…</span>
                </div>
              )}
            </div>
          </div>

          {/* Primary stream reference */}
          <div className="w-[35%] shrink-0">
            <div className="px-3 py-1.5 text-[10px] font-medium text-white/50">
              {primaryStreamId}
              <span className="ml-1.5 rounded bg-blue-500/20 px-1 py-px text-[9px] text-blue-400">
                Primary
              </span>
            </div>
            <div className="relative aspect-video w-full overflow-hidden">
              <canvas ref={primaryCanvasRef} className="h-full w-full object-contain" />
              {!framesCaptured && (
                <div className="absolute inset-0 flex items-center justify-center">
                  <span className="text-xs text-white/40">Loading…</span>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Stats bar */}
        <div className="flex items-center gap-6 border-t px-5 py-2.5 text-xs">
          <div className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-sm bg-amber-500" />
            <span className="text-muted">Before</span>
            <span className="font-mono">{Math.max(0, uncorrectedTime).toFixed(3)}s</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-sm bg-blue-500" />
            <span className="text-muted">After (corrected)</span>
            <span className="font-mono">{correctedTime.toFixed(3)}s</span>
          </div>
          <div className="flex-1" />
          <span className="text-muted">
            Correction:{" "}
            <span className="font-mono font-medium text-foreground">
              {sign}{absMs.toFixed(1)} ms
            </span>
            {fps > 0 && (
              <span className="ml-1 text-muted">
                ({Math.abs(driftMs / 1000 * fps).toFixed(1)} frames)
              </span>
            )}
          </span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Frame capture helpers
// ---------------------------------------------------------------------------

async function captureFrames(
  src: string,
  correctedTime: number,
  uncorrectedTime: number,
): Promise<{
  corrected: ImageBitmap;
  uncorrected: ImageBitmap;
  width: number;
  height: number;
}> {
  const video = document.createElement("video");
  video.crossOrigin = "anonymous";
  video.muted = true;
  video.preload = "auto";
  video.src = src;

  await new Promise<void>((resolve, reject) => {
    video.onloadeddata = () => resolve();
    video.onerror = () => reject(new Error("Video load failed"));
  });

  const corrected = await seekAndCapture(video, Math.max(0, correctedTime));
  const uncorrected = await seekAndCapture(video, Math.max(0, uncorrectedTime));

  return { corrected, uncorrected, width: video.videoWidth, height: video.videoHeight };
}

async function captureOneFrame(
  src: string,
  time: number,
): Promise<{ frame: ImageBitmap; width: number; height: number }> {
  const video = document.createElement("video");
  video.crossOrigin = "anonymous";
  video.muted = true;
  video.preload = "auto";
  video.src = src;

  await new Promise<void>((resolve, reject) => {
    video.onloadeddata = () => resolve();
    video.onerror = () => reject(new Error("Video load failed"));
  });

  const frame = await seekAndCapture(video, Math.max(0, time));
  return { frame, width: video.videoWidth, height: video.videoHeight };
}

function seekAndCapture(video: HTMLVideoElement, time: number): Promise<ImageBitmap> {
  return new Promise((resolve, reject) => {
    video.currentTime = time;
    video.onseeked = () => {
      createImageBitmap(video).then(resolve).catch(reject);
    };
    video.onerror = () => reject(new Error("Seek failed"));
  });
}

function drawToCanvas(
  canvas: HTMLCanvasElement | null,
  bitmap: ImageBitmap,
  width: number,
  height: number,
) {
  if (!canvas) return;
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  if (ctx) ctx.drawImage(bitmap, 0, 0, width, height);
}
