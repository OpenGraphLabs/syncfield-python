import { useState } from "react";

export function ConnectingOverlay() {
  return (
    <div className="flex items-center justify-center w-full aspect-video bg-slate-900/60 border border-slate-800 rounded">
      <div className="flex items-center gap-2 text-slate-300 text-sm">
        <span className="inline-block w-2 h-2 rounded-full bg-slate-400 animate-pulse" />
        Connecting…
      </div>
    </div>
  );
}

export function WaitingForDataOverlay() {
  return (
    <div className="flex items-center justify-center w-full aspect-video bg-yellow-900/20 border border-yellow-700/40 rounded">
      <div className="text-yellow-200 text-sm">
        Connected · waiting for first frame
      </div>
    </div>
  );
}

export function FailedOverlay({ error }: { error: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <button
      onClick={() => setExpanded((v) => !v)}
      className="w-full aspect-video bg-red-950/40 border border-red-500/40 rounded p-3 text-left cursor-pointer"
    >
      <div className="flex items-start gap-2">
        <span className="text-red-400 text-lg leading-none">⛔</span>
        <div className="flex-1 min-w-0">
          <div className="text-red-200 text-sm font-medium">
            Failed to connect
          </div>
          <div
            className={
              "mt-1 text-xs font-mono text-red-200/80 break-all " +
              (expanded ? "" : "line-clamp-2")
            }
          >
            {error}
          </div>
          <div className="mt-2 text-[11px] text-red-300/60">
            Press Discover Devices, or Disconnect + Connect to retry.
          </div>
        </div>
      </div>
    </button>
  );
}
