import { useCallback, useEffect, useState } from "react";
import type { FrameMapEntry } from "@/lib/review-types";

export interface DriftData {
  /** Time axis (seconds). */
  timesSec: number[];
  /** Per-frame max |delta_ms| after correction (residual). */
  afterDrift: number[];
  /** Per-frame estimated pre-correction drift (offset + residual). */
  beforeDrift: number[];
  /** Mean after-correction drift in ms. */
  meanAfterMs: number;
  /** Mean before-correction drift in ms. */
  meanBeforeMs: number;
  /** Improvement percentage. */
  improvementPct: number;
}

interface UseDriftDataReturn {
  driftData: DriftData | null;
  isLoading: boolean;
}

/**
 * Fetch frame_map.jsonl + sync_report.json and compute drift data.
 *
 * - **After drift**: max |delta_ms| across secondary streams per frame
 *   from frame_map.jsonl. This is the post-correction residual.
 * - **Before drift**: estimated by adding the sync correction offset
 *   (from sync_report.json) back to each frame's delta. Shows what
 *   the drift looked like before SyncField corrected it.
 */
export function useDriftData(episodeId: string | null): UseDriftDataReturn {
  const [driftData, setDriftData] = useState<DriftData | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const fetchData = useCallback(async () => {
    if (!episodeId) {
      setDriftData(null);
      return;
    }
    setIsLoading(true);
    try {
      // Fetch frame map and episode detail in parallel
      const [fmRes, epRes] = await Promise.all([
        fetch(`/api/episodes/${episodeId}/frame-map`),
        fetch(`/api/episodes/${episodeId}`),
      ]);

      if (!fmRes.ok || !epRes.ok) {
        setDriftData(null);
        return;
      }

      const fmData = await fmRes.json();
      const epData = await epRes.json();

      const entries: FrameMapEntry[] = fmData.frames ?? [];
      if (entries.length === 0) {
        setDriftData(null);
        return;
      }

      // Get the sync correction offset from the report
      const syncReport = epData.sync_report;
      const offsetMs: Record<string, number> = {};
      if (syncReport?.streams) {
        for (const [sid, info] of Object.entries(syncReport.streams)) {
          const s = info as { offset_ms?: number; role?: string };
          if (s.role !== "primary" && s.offset_ms != null) {
            offsetMs[sid] = s.offset_ms;
          }
        }
      }

      const timesSec: number[] = [];
      const afterDrift: number[] = [];
      const beforeDrift: number[] = [];

      for (const entry of entries) {
        timesSec.push(entry.primary_time_sec);

        const streamDeltas = Object.entries(entry.streams);
        if (streamDeltas.length === 0) {
          afterDrift.push(0);
          beforeDrift.push(0);
          continue;
        }

        // After: max |delta_ms| (the residual after correction)
        const maxAfter = Math.max(
          ...streamDeltas.map(([, s]) => Math.abs(s.delta_ms)),
        );
        afterDrift.push(maxAfter);

        // Before: add back the correction offset to estimate raw drift
        const maxBefore = Math.max(
          ...streamDeltas.map(([sid, s]) => {
            const correction = offsetMs[sid] ?? 0;
            return Math.abs(s.delta_ms + correction);
          }),
        );
        beforeDrift.push(maxBefore);
      }

      const meanAfterMs = mean(afterDrift);
      const meanBeforeMs = mean(beforeDrift);
      const improvementPct =
        meanBeforeMs > 0 ? (1 - meanAfterMs / meanBeforeMs) * 100 : 0;

      setDriftData({
        timesSec,
        afterDrift,
        beforeDrift,
        meanAfterMs,
        meanBeforeMs,
        improvementPct,
      });
    } catch {
      setDriftData(null);
    } finally {
      setIsLoading(false);
    }
  }, [episodeId]);

  useEffect(() => {
    void fetchData();
  }, [fetchData]);

  return { driftData, isLoading };
}

function mean(arr: number[]): number {
  if (arr.length === 0) return 0;
  return arr.reduce((a, b) => a + b, 0) / arr.length;
}
