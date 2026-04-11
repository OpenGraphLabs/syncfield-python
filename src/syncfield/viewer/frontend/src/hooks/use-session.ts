import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ControlAction,
  ServerMessage,
  SessionSnapshot,
} from "@/lib/types";
import { isCountdown, isSnapshot } from "@/lib/types";

export type ConnectionStatus = "connecting" | "connected" | "disconnected";

interface UseSessionReturn {
  snapshot: SessionSnapshot | null;
  countdown: number | null;
  sendCommand: (action: ControlAction, data?: Record<string, unknown>) => void;
  connectionStatus: ConnectionStatus;
}

const RECONNECT_DELAY_MS = 2000;

/**
 * WebSocket hook for session state and control commands.
 *
 * Connects to `/ws/control`, receives 10 Hz snapshot broadcasts and
 * countdown events, and sends control commands back to the server.
 * Automatically reconnects on disconnect.
 */
export function useSession(): UseSessionReturn {
  const [snapshot, setSnapshot] = useState<SessionSnapshot | null>(null);
  const [countdown, setCountdown] = useState<number | null>(null);
  const [connectionStatus, setConnectionStatus] =
    useState<ConnectionStatus>("connecting");

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  // Clear countdown after it reaches 0 (recording has started)
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
          // Clear countdown display after the last tick
          if (countdownTimeout.current) clearTimeout(countdownTimeout.current);
          if (msg.count === 1) {
            countdownTimeout.current = setTimeout(() => {
              if (mountedRef.current) setCountdown(null);
            }, 1000);
          }
        }
      } catch {
        // Ignore malformed messages
      }
    };

    ws.onclose = () => {
      if (!mountedRef.current) return;
      setConnectionStatus("disconnected");
      wsRef.current = null;
      // Auto-reconnect
      reconnectTimer.current = setTimeout(connect, RECONNECT_DELAY_MS);
    };

    ws.onerror = () => {
      // onclose will fire after onerror — reconnect is handled there
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
        wsRef.current.onclose = null; // Prevent reconnect on unmount
        wsRef.current.close();
      }
    };
  }, [connect]);

  const sendCommand = useCallback(
    (action: ControlAction, data?: Record<string, unknown>) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action, ...data }));
      }
    },
    [],
  );

  return { snapshot, countdown, sendCommand, connectionStatus };
}
