import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ControlAction,
  ServerMessage,
  SessionSnapshot,
  StopResultEvent,
} from "@/lib/types";
import { isCountdown, isSnapshot, isStopResult } from "@/lib/types";

export type ConnectionStatus = "connecting" | "connected" | "disconnected";

export interface CollectResultEvent {
  type: "aggregate_all_pending_result";
  ok: boolean;
  enqueued?: string[];
  skipped?: Array<{ episode_id?: string; path?: string; reason: string }>;
  error?: string;
}

interface UseSessionReturn {
  snapshot: SessionSnapshot | null;
  countdown: number | null;
  stopResult: StopResultEvent | null;
  collectResult: CollectResultEvent | null;
  sendCommand: (action: ControlAction, data?: Record<string, unknown>) => void;
  dismissStopResult: () => void;
  dismissCollectResult: () => void;
  connectionStatus: ConnectionStatus;
}

const RECONNECT_DELAY_MS = 2000;

/**
 * WebSocket hook for session state and control commands.
 *
 * Connects to `/ws/control`, receives 10 Hz snapshot broadcasts,
 * countdown events, and stop result events with per-stream validation.
 */
export function useSession(): UseSessionReturn {
  const [snapshot, setSnapshot] = useState<SessionSnapshot | null>(null);
  const [countdown, setCountdown] = useState<number | null>(null);
  const [stopResult, setStopResult] = useState<StopResultEvent | null>(null);
  const [collectResult, setCollectResult] = useState<CollectResultEvent | null>(null);
  const [connectionStatus, setConnectionStatus] =
    useState<ConnectionStatus>("connecting");

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const countdownTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/ws/control`;

    setConnectionStatus("connecting");
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      if (!mountedRef.current) return;
      setConnectionStatus("connected");
    };

    ws.onmessage = (event) => {
      if (!mountedRef.current) return;
      try {
        const msg: ServerMessage = JSON.parse(event.data);
        if (isSnapshot(msg)) {
          setSnapshot(msg);
        } else if (isCountdown(msg)) {
          setCountdown(msg.count);
          if (countdownTimeout.current) clearTimeout(countdownTimeout.current);
          if (msg.count === 1) {
            countdownTimeout.current = setTimeout(() => {
              if (mountedRef.current) setCountdown(null);
            }, 1000);
          }
        } else if (isStopResult(msg)) {
          setStopResult(msg);
        } else if (
          (msg as CollectResultEvent).type === "aggregate_all_pending_result"
        ) {
          setCollectResult(msg as CollectResultEvent);
        }
      } catch {
        // Ignore malformed messages
      }
    };

    ws.onclose = () => {
      if (!mountedRef.current) return;
      setConnectionStatus("disconnected");
      wsRef.current = null;
      reconnectTimer.current = setTimeout(connect, RECONNECT_DELAY_MS);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    connect();

    return () => {
      mountedRef.current = false;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      if (countdownTimeout.current) clearTimeout(countdownTimeout.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
    };
  }, [connect]);

  const sendCommand = useCallback(
    (action: ControlAction, data?: Record<string, unknown>) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        // Clear previous stop result when starting a new recording
        if (action === "record") setStopResult(null);
        ws.send(JSON.stringify({ action, ...data }));
      }
    },
    [],
  );

  const dismissStopResult = useCallback(() => {
    setStopResult(null);
  }, []);
  const dismissCollectResult = useCallback(() => {
    setCollectResult(null);
  }, []);

  return {
    snapshot,
    countdown,
    stopResult,
    collectResult,
    dismissCollectResult,
    sendCommand,
    dismissStopResult,
    connectionStatus,
  };
}
