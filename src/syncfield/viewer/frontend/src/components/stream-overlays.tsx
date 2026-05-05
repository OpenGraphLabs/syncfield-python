import { useState } from "react";
import { cn } from "@/lib/utils";

type OverlayVariant = "aspect" | "fill";

function overlaySizing(variant: OverlayVariant): string {
  return variant === "fill" ? "h-full w-full" : "aspect-video w-full";
}

export function ConnectingOverlay({ variant = "aspect" }: { variant?: OverlayVariant } = {}) {
  return (
    <div className={`flex ${overlaySizing(variant)} items-center justify-center rounded border bg-background-subtle`}>
      <div className="flex items-center gap-2 text-xs text-muted">
        <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-muted" />
        Connecting…
      </div>
    </div>
  );
}

export function WaitingForDataOverlay({ variant = "aspect" }: { variant?: OverlayVariant } = {}) {
  return (
    <div className={`flex ${overlaySizing(variant)} items-center justify-center rounded border bg-background-subtle`}>
      <div className="flex items-center gap-2 text-xs text-muted">
        <span className="inline-block h-1.5 w-1.5 rounded-full bg-warning" />
        Connected · waiting for first frame
      </div>
    </div>
  );
}

export function FailedOverlay({
  error,
  variant = "aspect",
}: {
  error: string;
  variant?: OverlayVariant;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className={`flex ${overlaySizing(variant)} flex-col overflow-hidden rounded border border-destructive/30 bg-card`}>
      <div className="flex shrink-0 items-center gap-2 border-b border-destructive/20 px-3 py-2">
        <svg
          width="14"
          height="14"
          viewBox="0 0 14 14"
          fill="none"
          className="shrink-0 text-destructive"
          aria-hidden
        >
          <circle cx="7" cy="7" r="6" stroke="currentColor" strokeWidth="1.25" />
          <path d="M7 4v3.5" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" />
          <circle cx="7" cy="9.75" r="0.6" fill="currentColor" />
        </svg>
        <span className="text-xs font-medium text-foreground">Failed to connect</span>
      </div>

      <pre
        className={cn(
          "min-h-0 flex-1 overflow-y-auto px-3 py-2 font-mono text-[11px] leading-relaxed text-muted",
          "select-text whitespace-pre-wrap break-words",
          !expanded && "line-clamp-4",
        )}
      >
        {error}
      </pre>

      <div className="flex shrink-0 items-center justify-between gap-2 border-t border-destructive/15 px-3 py-1.5">
        <span className="truncate text-[10px] text-muted">
          Try Discover Devices, or Disconnect → Connect.
        </span>
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium text-muted hover:bg-foreground/5 hover:text-foreground"
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      </div>
    </div>
  );
}
