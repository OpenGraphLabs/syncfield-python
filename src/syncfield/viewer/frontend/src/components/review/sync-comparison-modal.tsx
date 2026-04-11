import { useCallback, useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

interface SyncComparisonModalProps {
  streamId: string;
  episodeId: string;
  driftMs: number;
  fps: number;
  currentTime: number;
  onClose: () => void;
}

/**
 * Sync before/after comparison modal.
 *
 * Shows two frames from the secondary stream side-by-side with a
 * draggable slider to reveal the difference:
 * - **Before** (amber): where the frame was without sync correction
 * - **After** (blue): where the frame is after correction
 *
 * Ported from opengraph-studio/analyzer's SyncComparisonModal.
 */
export function SyncComparisonModal({
  streamId,
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

  // Canvas refs for before/after frames
  const beforeCanvasRef = useRef<HTMLCanvasElement>(null);
  const afterCanvasRef = useRef<HTMLCanvasElement>(null);
  const [framesCaptured, setFramesCaptured] = useState(false);

  const driftSec = driftMs / 1000;
  const correctedTime = currentTime;
  const uncorrectedTime = currentTime + driftSec;

  // Capture frames from the video at corrected and uncorrected positions
  useEffect(() => {
    const videoSrc = `/api/episodes/${episodeId}/video/${streamId}.mp4`;
    captureFrames(videoSrc, correctedTime, uncorrectedTime).then(
      ({ corrected, uncorrected, width, height }) => {
        drawToCanvas(afterCanvasRef.current, corrected, width, height);
        drawToCanvas(beforeCanvasRef.current, uncorrected, width, height);
        setFramesCaptured(true);
      },
    ).catch(() => {
      // Frame capture failed — show placeholder
    });
  }, [episodeId, streamId, correctedTime, uncorrectedTime]);

  // Slider drag handling
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

  // Keyboard: Escape to close
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
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* Modal */}
      <div className="relative w-full max-w-4xl rounded-2xl border bg-card shadow-2xl">
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
            {sign}{absMs.toFixed(1)} ms drift
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

        {/* Comparison area */}
        <div
          ref={containerRef}
          className="relative aspect-video w-full overflow-hidden bg-black"
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
        >
          {/* After (corrected) — full width background */}
          <canvas
            ref={afterCanvasRef}
            className="absolute inset-0 h-full w-full object-contain"
          />

          {/* Before (uncorrected) — clipped by slider */}
          <div
            className="absolute inset-0"
            style={{ clipPath: `inset(0 ${100 - sliderPct}% 0 0)` }}
          >
            <canvas
              ref={beforeCanvasRef}
              className="h-full w-full object-contain"
            />
          </div>

          {/* Slider line */}
          <div
            className="absolute top-0 bottom-0 w-0.5 bg-white shadow-lg"
            style={{ left: `${sliderPct}%` }}
          >
            {/* Handle */}
            <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-white p-1.5 shadow-md">
              <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
                <path d="M5 3L2 8L5 13" stroke="#333" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                <path d="M11 3L14 8L11 13" stroke="#333" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </div>
          </div>

          {/* Labels */}
          <div
            className="absolute left-3 top-3 rounded-md bg-amber-500/80 px-2 py-0.5 text-[11px] font-semibold text-white transition-opacity"
            style={{ opacity: sliderPct > 15 ? 1 : 0 }}
          >
            Before
          </div>
          <div
            className="absolute right-3 top-3 rounded-md bg-blue-500/80 px-2 py-0.5 text-[11px] font-semibold text-white transition-opacity"
            style={{ opacity: sliderPct < 85 ? 1 : 0 }}
          >
            After
          </div>

          {/* Drag hint */}
          {showHint && framesCaptured && (
            <div className="absolute inset-x-0 bottom-4 flex justify-center">
              <span className="rounded-full bg-black/60 px-3 py-1 text-[11px] text-white/80">
                ← Drag to compare →
              </span>
            </div>
          )}

          {/* Loading state */}
          {!framesCaptured && (
            <div className="absolute inset-0 flex items-center justify-center">
              <span className="text-xs text-white/50">Capturing frames…</span>
            </div>
          )}
        </div>

        {/* Stats bar */}
        <div className="flex items-center gap-6 border-t px-5 py-2.5 text-xs">
          <div className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-sm bg-amber-500" />
            <span className="text-muted">Before</span>
            <span className="font-mono">
              {uncorrectedTime.toFixed(3)}s
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-sm bg-blue-500" />
            <span className="text-muted">After</span>
            <span className="font-mono">
              {correctedTime.toFixed(3)}s
            </span>
          </div>
          <div className="flex-1" />
          <div className="text-muted">
            Correction: <span className="font-mono font-medium text-foreground">{sign}{absMs.toFixed(1)} ms</span>
            {fps > 0 && (
              <span className="ml-1">
                ({Math.abs(driftMs / 1000 * fps).toFixed(1)} frames)
              </span>
            )}
          </div>
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

  return {
    corrected,
    uncorrected,
    width: video.videoWidth,
    height: video.videoHeight,
  };
}

function seekAndCapture(
  video: HTMLVideoElement,
  time: number,
): Promise<ImageBitmap> {
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
