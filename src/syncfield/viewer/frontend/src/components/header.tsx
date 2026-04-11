import type { SessionSnapshot } from "@/lib/types";
import { formatElapsed } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Logo } from "./logo";

interface HeaderProps {
  snapshot: SessionSnapshot | null;
  onDiscoverClick: () => void;
}

/** Dot color by session state. */
const STATE_DOT: Record<string, string> = {
  recording: "bg-recording animate-pulse-recording",
  countdown: "bg-warning",
  preparing: "bg-warning",
  connecting: "bg-warning",
  disconnecting: "bg-warning",
  stopping: "bg-warning",
};

/** User-friendly labels — idle-like states show "Ready". */
function friendlyState(state: string): string {
  switch (state) {
    case "idle":
    case "connected":
    case "stopped":
      return "Ready";
    case "recording":
      return "Recording";
    case "countdown":
    case "preparing":
      return "Starting…";
    case "connecting":
      return "Connecting…";
    case "stopping":
      return "Saving…";
    case "disconnecting":
      return "Disconnecting…";
    default:
      return state;
  }
}

export function Header({ snapshot, onDiscoverClick }: HeaderProps) {
  const state = snapshot?.state ?? "idle";
  const hostId = snapshot?.host_id ?? "—";
  const elapsed = snapshot?.elapsed_s ?? 0;
  const isRecording = state === "recording";

  return (
    <header
      className={cn(
        "flex h-12 items-center gap-4 border-b px-4 transition-colors",
        isRecording && "border-recording/30 bg-recording/5",
      )}
    >
      {/* OpenGraph Labs logo */}
      <Logo className="h-4 shrink-0" />

      <div className="mx-1 h-4 w-px bg-border" />

      {/* Host ID */}
      <span className="font-mono text-xs text-muted">{hostId}</span>

      <div className="mx-1 h-4 w-px bg-border" />

      {/* State indicator */}
      <div className="flex items-center gap-1.5">
        <span
          className={cn(
            "inline-block h-2 w-2 rounded-full",
            STATE_DOT[state] ?? "bg-success",
          )}
        />
        <span
          className={cn(
            "text-xs font-medium",
            isRecording ? "text-recording" : "text-muted",
          )}
        >
          {friendlyState(state)}
        </span>
      </div>

      {/* Elapsed timer */}
      {isRecording && (
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
        disabled={isRecording || state === "starting"}
      >
        Discover Devices
      </button>
    </header>
  );
}
