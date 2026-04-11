import type { ControlAction, SessionState } from "@/lib/types";
import { cn } from "@/lib/utils";

interface ControlPanelProps {
  state: SessionState;
  onCommand: (action: ControlAction, data?: Record<string, unknown>) => void;
}

/**
 * Session control buttons — Connect, Disconnect, Record, Stop, Cancel.
 *
 * Button enable/disable logic mirrors the DearPyGui viewer exactly:
 * each action is only available in the states where it makes sense.
 */
export function ControlPanel({ state, onCommand }: ControlPanelProps) {
  const canConnect = state === "idle" || state === "stopped";
  const canDisconnect = state === "connected" || state === "stopped";
  const canRecord = state === "connected";
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

// ---------------------------------------------------------------------------
// Internal button component (thin wrapper, not a shared UI primitive)
// ---------------------------------------------------------------------------

type ButtonVariant = "default" | "primary" | "destructive" | "ghost";

function Button({
  children,
  onClick,
  disabled,
  variant = "default",
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled: boolean;
  variant?: ButtonVariant;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
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
