import { useEffect, useRef, useState } from "react";
import { sensorStore } from "@/lib/sensor-store";

const DEFAULT_MAX_POINTS = 300;

interface UseSensorStreamReturn {
  /** Per-channel rolling buffer of values. */
  channels: Record<string, number[]>;
  /** Rolling buffer of x-axis labels (timestamps). */
  labels: number[];
  /** Latest vector-valued channels (e.g. ``hand_joints`` from
   * MetaQuestHandStream). Not buffered — 3-D pose panels render
   * instantaneous state. ``null`` when the stream is scalar-only. */
  pose: Record<string, number[]> | null;
  /** Whether the shared SSE connection is alive. */
  isConnected: boolean;
}

interface UseSensorStreamOptions {
  maxPoints?: number;
}

/**
 * Subscribe to real-time sensor data for one stream.
 *
 * Reads from the shared :module:`sensor-store` rather than opening a
 * per-hook EventSource, so N tiles share one HTTP connection and stay
 * under the browser's HTTP/1.1 6-per-origin cap. The returned buffers
 * are local to this hook — each consumer keeps its own rolling window.
 */
export function useSensorStream(
  streamId: string,
  options: UseSensorStreamOptions = {},
): UseSensorStreamReturn {
  const [channels, setChannels] = useState<Record<string, number[]>>({});
  const [labels, setLabels] = useState<number[]>([]);
  const [pose, setPose] = useState<Record<string, number[]> | null>(null);
  const [isConnected, setIsConnected] = useState(false);

  const channelBuf = useRef<Record<string, number[]>>({});
  const labelBuf = useRef<number[]>([]);
  const maxPointsRef = useRef(DEFAULT_MAX_POINTS);

  useEffect(() => {
    maxPointsRef.current = Math.max(
      30,
      Math.round(options.maxPoints ?? DEFAULT_MAX_POINTS),
    );
  }, [options.maxPoints]);

  useEffect(() => {
    channelBuf.current = {};
    labelBuf.current = [];
    setChannels({});
    setLabels([]);
    setPose(null);

    const unsub = sensorStore.subscribe(streamId, (data) => {
      if (data.label !== null && data.label !== undefined) {
        labelBuf.current.push(data.label);
        if (labelBuf.current.length > maxPointsRef.current) {
          labelBuf.current = labelBuf.current.slice(-maxPointsRef.current);
        }
      }
      for (const [name, value] of Object.entries(data.channels)) {
        if (!channelBuf.current[name]) channelBuf.current[name] = [];
        channelBuf.current[name].push(value);
        if (channelBuf.current[name].length > maxPointsRef.current) {
          channelBuf.current[name] = channelBuf.current[name].slice(-maxPointsRef.current);
        }
      }
      if (data.pose) setPose(data.pose);
      setChannels({ ...channelBuf.current });
      setLabels([...labelBuf.current]);
    });

    const unsubStatus = sensorStore.subscribeStatus(setIsConnected);

    return () => {
      unsub();
      unsubStatus();
    };
  }, [streamId]);

  return { channels, labels, pose, isConnected };
}
