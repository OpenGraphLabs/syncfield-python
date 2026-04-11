import type { SessionSnapshot } from "@/lib/types";
import { formatElapsed, stateLabel } from "@/lib/format";
import { cn } from "@/lib/utils";

interface HeaderProps {
  snapshot: SessionSnapshot | null;
  onDiscoverClick: () => void;
}

const STATE_COLORS: Record<string, string> = {
  idle: "bg-muted",
  connecting: "bg-warning",
  connected: "bg-success",
  starting: "bg-warning",
  recording: "bg-recording animate-pulse-recording",
  stopping: "bg-warning",
  stopped: "bg-muted",
  disconnecting: "bg-warning",
};

export function Header({ snapshot, onDiscoverClick }: HeaderProps) {
  const state = snapshot?.state ?? "idle";
  const hostId = snapshot?.host_id ?? "—";
  const elapsed = snapshot?.elapsed_s ?? 0;

  return (
    <header className="flex h-12 items-center gap-4 border-b px-4">
      {/* Logo */}
      <h1 className="text-sm font-semibold tracking-tight">SyncField</h1>

      <div className="mx-1 h-4 w-px bg-border" />

      {/* Host ID */}
      <span className="font-mono text-xs text-muted">{hostId}</span>

      <div className="mx-1 h-4 w-px bg-border" />

      {/* State indicator */}
      <div className="flex items-center gap-1.5">
        <span
          className={cn("inline-block h-2 w-2 rounded-full", STATE_COLORS[state])}
        />
        <span
          className={cn(
            "text-xs font-medium",
            state === "recording" ? "text-recording" : "text-muted",
          )}
        >
          {stateLabel(state)}
        </span>
      </div>

      {/* Elapsed timer */}
      {state === "recording" && (
        <>
          <div className="mx-1 h-4 w-px bg-border" />
          <span className="font-mono text-xs tabular-nums text-recording">
            {formatElapsed(elapsed)}
          </span>
        </>
      )}

      <div className="flex-1" />

      {/* Discover devices button */}
      <button
        onClick={onDiscoverClick}
        className={cn(
          "rounded-lg border px-3 py-1 text-xs font-medium",
          "transition-colors hover:bg-foreground/5",
          "disabled:cursor-not-allowed disabled:opacity-50",
        )}
        disabled={state === "recording" || state === "starting"}
      >
        Discover Devices
      </button>
    </header>
  );
}
