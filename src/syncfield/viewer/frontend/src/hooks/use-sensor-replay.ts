import { useEffect, useState } from "react";

export interface SensorReplayData {
  /** Sample timestamps in seconds, relative to the first sample. */
  t: number[];
  /** Per-channel scalar value arrays aligned to ``t``. */
  channels: Record<string, number[]>;
  /** Per-channel vector samples aligned to ``t`` (e.g. Quest's
   *  156-float ``hand_joints``). Optional for older payloads. */
  vector_channels?: Record<string, number[][]>;
  /** Total number of samples in the source file (before decimation). */
  count: number;
  /** Duration in seconds from first to last sample. */
  duration_s: number;
}

interface UseSensorReplayReturn {
  data: SensorReplayData | null;
  isLoading: boolean;
  error: string | null;
}

/**
 * Fetch recorded sensor samples for an episode, suitable for Review
 * mode playback. The server decimates long recordings server-side so
 * the payload stays small; all consumers just need to binary-search
 * ``t`` for the index that matches ``<video>.currentTime``.
 */
export function useSensorReplay(
  episodeId: string,
  streamId: string,
): UseSensorReplayReturn {
  const [data, setData] = useState<SensorReplayData | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    setIsLoading(true);
    setError(null);
    fetch(`/api/episodes/${episodeId}/sensor/${streamId}`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return (await res.json()) as SensorReplayData;
      })
      .then((json) => {
        if (disposed) return;
        setData(json);
      })
      .catch((err) => {
        if (disposed) return;
        setError(err instanceof Error ? err.message : String(err));
        setData(null);
      })
      .finally(() => {
        if (!disposed) setIsLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [episodeId, streamId]);

  return { data, isLoading, error };
}

/**
 * Binary-search for the sample index whose timestamp is closest to
 * ``targetSeconds``. Returns 0 for an empty array. Runs in O(log n)
 * so it's cheap to call per animation frame.
 */
export function sampleIndexAt(t: number[], targetSeconds: number): number {
  const n = t.length;
  if (n === 0) return 0;
  if (targetSeconds <= t[0]!) return 0;
  if (targetSeconds >= t[n - 1]!) return n - 1;

  let lo = 0;
  let hi = n - 1;
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1;
    if (t[mid]! <= targetSeconds) lo = mid;
    else hi = mid;
  }
  // Pick whichever of lo/hi is closer to the target.
  return targetSeconds - t[lo]! <= t[hi]! - targetSeconds ? lo : hi;
}
