import type { ControlAction, SessionState } from "@/lib/types";
import { cn } from "@/lib/utils";

interface ControlPanelProps {
  state: SessionState;
  hasTask: boolean;
  onCommand: (action: ControlAction, data?: Record<string, unknown>) => void;
}

/**
 * Session control buttons — Connect, Disconnect, Record, Stop, Cancel.
 *
 * Go3S video collection lives on the Review page (USB-based), not here.
 *
 * Record is disabled unless a task is selected (hasTask=true).
 * Cancel stops recording and discards the episode.
 */
export function ControlPanel({
  state,
  hasTask,
  onCommand,
}: ControlPanelProps) {
  const canConnect = state === "idle" || state === "stopped";
  const canDisconnect = state === "connected" || state === "stopped";
  const canRecord = state === "connected" && hasTask;
  const canStop = state === "recording";
  const canCancel = state === "recording" || state === "stopping";

  return (
    <div className="flex items-center gap-2 border-b px-4 py-2">
      {/* Connection group */}
      <Button
        onClick={() => onCommand("connect")}
        disabled={!canConnect}
        variant="default"
      >
        Connect
      </Button>
      <Button
        onClick={() => onCommand("disconnect")}
        disabled={!canDisconnect}
        variant="ghost"
      >
        Disconnect
      </Button>

      <div className="mx-2 h-4 w-px bg-border" />

      {/* Recording group */}
      <Button
        onClick={() => onCommand("record", { countdown_s: 3 })}
        disabled={!canRecord}
        variant="primary"
      >
        Record
      </Button>
      <Button
        onClick={() => onCommand("stop")}
        disabled={!canStop}
        variant="destructive"
      >
        Stop
      </Button>
      <Button
        onClick={() => onCommand("cancel")}
        disabled={!canCancel}
        variant="ghost"
      >
        Cancel
      </Button>
    </div>
  );
}

type ButtonVariant = "default" | "primary" | "destructive" | "ghost";

function Button({
  children,
  onClick,
  disabled,
  variant = "default",
  title,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled: boolean;
  variant?: ButtonVariant;
  title?: string;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={cn(
        "rounded-lg px-3 py-1.5 text-xs font-medium transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-40",
        variant === "primary" &&
          "bg-primary text-primary-foreground hover:bg-primary/90",
        variant === "destructive" &&
          "bg-destructive text-destructive-foreground hover:bg-destructive/90",
        variant === "ghost" && "border hover:bg-foreground/5",
        variant === "default" && "border bg-card hover:bg-foreground/5",
      )}
    >
      {children}
    </button>
  );
}
