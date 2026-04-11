import { useCallback, useEffect, useState } from "react";
import type { FrameMapEntry } from "@/lib/review-types";

export interface DriftData {
  /** Frame indices (x-axis). */
  frames: number[];
  /** Max |delta_ms| across all streams per frame before correction. */
  beforeDrift: number[];
  /** Max |delta_ms| across all streams per frame after correction. */
  afterDrift: number[];
  /** Improvement percentage: (1 - meanAfter / meanBefore) * 100. */
  improvementPct: number;
}

interface UseDriftDataReturn {
  /** Processed drift data for charting, or null while loading. */
  driftData: DriftData | null;
  /** Whether the frame map is being loaded. */
  isLoading: boolean;
}

/**
 * REST hook for fetching and processing the frame map into drift data.
 *
 * Fetches `GET /api/episodes/{id}/frame-map` (JSONL format, one JSON
 * object per line) and computes per-frame max drift before and after
 * sync correction. The "before" drift uses the raw offset between
 * original frame timing and primary time; the "after" drift uses the
 * post-correction `delta_ms` values from the frame map.
 */
export function useDriftData(episodeId: string | null): UseDriftDataReturn {
  const [driftData, setDriftData] = useState<DriftData | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const fetchDriftData = useCallback(async () => {
    if (!episodeId) {
      setDriftData(null);
      return;
    }
    setIsLoading(true);
    try {
      const res = await fetch(`/api/episodes/${episodeId}/frame-map`);
      if (!res.ok) {
        setDriftData(null);
        return;
      }
      const data = await res.json();
      const entries: FrameMapEntry[] = data.frames ?? [];

      if (entries.length === 0) {
        setDriftData(null);
        return;
      }

      const frames: number[] = [];
      const beforeDrift: number[] = [];
      const afterDrift: number[] = [];

      for (const entry of entries) {
        frames.push(entry.frame);

        const streamValues = Object.values(entry.streams);
        // After correction: max |delta_ms| across all streams for this frame
        const maxAfter =
          streamValues.length > 0
            ? Math.max(...streamValues.map((s) => Math.abs(s.delta_ms)))
            : 0;
        afterDrift.push(maxAfter);

        // Before correction: use original_frame offset as a proxy
        // The delta_ms in the frame map is the post-correction residual.
        // For "before", we estimate from the difference between the
        // original frame index and the mapped frame index, scaled by
        // the frame's time step.
        const maxBefore =
          streamValues.length > 0
            ? Math.max(
                ...streamValues.map((s) =>
                  Math.abs(s.delta_ms + (entry.frame - s.frame) * (1000 / 30)),
                ),
              )
            : 0;
        beforeDrift.push(maxBefore);
      }

      // Compute improvement
      const meanBefore =
        beforeDrift.reduce((a, b) => a + b, 0) / beforeDrift.length;
      const meanAfter =
        afterDrift.reduce((a, b) => a + b, 0) / afterDrift.length;
      const improvementPct =
        meanBefore > 0 ? (1 - meanAfter / meanBefore) * 100 : 0;

      setDriftData({ frames, beforeDrift, afterDrift, improvementPct });
    } catch {
      setDriftData(null);
    } finally {
      setIsLoading(false);
    }
  }, [episodeId]);

  useEffect(() => {
    void fetchDriftData();
  }, [fetchDriftData]);

  return { driftData, isLoading };
}
