import { useEffect, useRef, useState } from "react";
import type { SensorEvent } from "@/lib/types";

const MAX_POINTS = 300;
const RECONNECT_DELAY_MS = 2000;

interface UseSensorStreamReturn {
  /** Per-channel rolling buffer of values. */
  channels: Record<string, number[]>;
  /** Rolling buffer of x-axis labels (timestamps). */
  labels: number[];
  /** Latest vector-valued channels (e.g. ``hand_joints`` from
   * MetaQuestHandStream). Not buffered — 3-D pose panels render
   * instantaneous state. ``null`` when the stream is scalar-only. */
  pose: Record<string, number[]> | null;
  /** Whether the SSE connection is alive. */
  isConnected: boolean;
}

/**
 * SSE hook for real-time sensor channel data.
 *
 * Connects to `/stream/sensor/{streamId}` and maintains a rolling
 * buffer (max 300 points) per channel. Automatically reconnects on
 * disconnect.
 */
export function useSensorStream(streamId: string): UseSensorStreamReturn {
  const [channels, setChannels] = useState<Record<string, number[]>>({});
  const [labels, setLabels] = useState<number[]>([]);
  const [pose, setPose] = useState<Record<string, number[]> | null>(null);
  const [isConnected, setIsConnected] = useState(false);

  // Mutable buffers for performance — we only push state on ticks
  const channelBuf = useRef<Record<string, number[]>>({});
  const labelBuf = useRef<number[]>([]);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    function connect() {
      if (!mountedRef.current) return;

      es = new EventSource(`/stream/sensor/${streamId}`);

      es.onopen = () => {
        if (!mountedRef.current) return;
        setIsConnected(true);
      };

      es.onmessage = (event) => {
        if (!mountedRef.current) return;
        try {
          const data: SensorEvent = JSON.parse(event.data);

          // Append to label buffer
          if (data.label !== null) {
            labelBuf.current.push(data.label);
            if (labelBuf.current.length > MAX_POINTS) {
              labelBuf.current = labelBuf.current.slice(-MAX_POINTS);
            }
          }

          // Append to each channel buffer
          for (const [name, value] of Object.entries(data.channels)) {
            if (!channelBuf.current[name]) {
              channelBuf.current[name] = [];
            }
            channelBuf.current[name].push(value);
            if (channelBuf.current[name].length > MAX_POINTS) {
              channelBuf.current[name] = channelBuf.current[name].slice(
                -MAX_POINTS,
              );
            }
          }

          // Vector-valued channels: ship the entire latest payload
          // through — not buffered because pose panels want a single
          // instantaneous snapshot per render.
          if (data.pose) setPose(data.pose);

          // Push a snapshot to React state
          setChannels({ ...channelBuf.current });
          setLabels([...labelBuf.current]);
        } catch {
          // Ignore malformed events
        }
      };

      es.onerror = () => {
        if (!mountedRef.current) return;
        setIsConnected(false);
        es?.close();
        es = null;
        reconnectTimer = setTimeout(connect, RECONNECT_DELAY_MS);
      };
    }

    connect();

    return () => {
      mountedRef.current = false;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (es) es.close();
      channelBuf.current = {};
      labelBuf.current = [];
    };
  }, [streamId]);

  return { channels, labels, pose, isConnected };
}
