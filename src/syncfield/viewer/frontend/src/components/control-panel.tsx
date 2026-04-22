import { useEffect, useRef, useState } from "react";
import type { ControlAction, SessionState } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Spinner } from "./spinner";

interface ControlPanelProps {
  state: SessionState;
  hasTask: boolean;
  onCommand: (action: ControlAction, data?: Record<string, unknown>) => void;
}

/**
 * Session control buttons — Connect, Disconnect, Record, Stop, Cancel.
 *
 * Uses an optimistic "pending" marker so a clicked button switches to
 * its loading state immediately, without waiting for the server to
 * echo a transitional SessionState back over the websocket. Once the
 * server-reported state moves past the moment of click, the marker
 * clears and the server's real state drives the UI.
 */
export function ControlPanel({
  state,
  hasTask,
  onCommand,
}: ControlPanelProps) {
  const [pending, setPending] = useState<ControlAction | null>(null);
  const pendingFromRef = useRef<SessionState | null>(null);

  // Drop the optimistic marker the moment the server's state changes
  // — the real transitional state ("disconnecting", "connecting", …)
  // then takes over driving the busy UI.
  useEffect(() => {
    if (!pending) return;
    if (pendingFromRef.current !== null && state !== pendingFromRef.current) {
      setPending(null);
    }
  }, [state, pending]);

  // Safety net: if the server never echoes back (network hiccup),
  // un-stick the button after a generous timeout.
  useEffect(() => {
    if (!pending) return;
    const t = setTimeout(() => setPending(null), 15000);
    return () => clearTimeout(t);
  }, [pending]);

  const dispatch = (action: ControlAction, data?: Record<string, unknown>) => {
    pendingFromRef.current = state;
    setPending(action);
    onCommand(action, data);
  };

  // A button is "busy" whenever the server is in the matching transitional
  // state OR this client just fired the command (optimistic).
  const isConnectBusy = pending === "connect" || state === "connecting";
  const isDisconnectBusy =
    pending === "disconnect" || state === "disconnecting";
  const isRecordBusy = pending === "record" || state === "starting";
  const isStopBusy = pending === "stop" || state === "stopping";

  const canConnect = state === "idle" || state === "stopped";
  const canDisconnect = state === "connected" || state === "stopped";
  const canRecord = state === "connected" && hasTask;
  const canStop = state === "recording";
  const canCancel = state === "recording" || state === "stopping";

  return (
    <div className="flex items-center gap-2 border-b px-4 py-2">
      {/* Connection group */}
      <Button
        onClick={() => dispatch("connect")}
        disabled={!canConnect}
        loading={isConnectBusy}
        loadingLabel="Connecting…"
        variant="default"
      >
        Connect
      </Button>
      <Button
        onClick={() => dispatch("disconnect")}
        disabled={!canDisconnect}
        loading={isDisconnectBusy}
        loadingLabel="Disconnecting…"
        variant="ghost"
      >
        Disconnect
      </Button>

      <div className="mx-2 h-4 w-px bg-border" />

      {/* Recording group */}
      <Button
        onClick={() => dispatch("record", { countdown_s: 3 })}
        disabled={!canRecord}
        loading={isRecordBusy}
        loadingLabel="Starting…"
        variant="primary"
      >
        Record
      </Button>
      <Button
        onClick={() => dispatch("stop")}
        disabled={!canStop}
        loading={isStopBusy}
        loadingLabel="Stopping…"
        variant="destructive"
      >
        Stop
      </Button>
      <Button
        onClick={() => dispatch("cancel")}
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
  loading = false,
  loadingLabel,
  variant = "default",
  title,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled: boolean;
  loading?: boolean;
  loadingLabel?: string;
  variant?: ButtonVariant;
  title?: string;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled || loading}
      title={title}
      aria-busy={loading || undefined}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-40",
        variant === "primary" &&
          "bg-primary text-primary-foreground hover:bg-primary/90",
        variant === "destructive" &&
          "bg-destructive text-destructive-foreground hover:bg-destructive/90",
        variant === "ghost" && "border hover:bg-foreground/5",
        variant === "default" && "border bg-card hover:bg-foreground/5",
      )}
    >
      {loading && <Spinner className="h-3 w-3" />}
      <span>{loading && loadingLabel ? loadingLabel : children}</span>
    </button>
  );
}
