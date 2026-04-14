import { useMemo } from "react";
import { PosePanel } from "@/components/pose-panel";
import { sampleIndexAt, useSensorReplay } from "@/hooks/use-sensor-replay";
import { ReviewSensorChart } from "./review-sensor-chart";

interface ReviewSensorPanelProps {
  episodeId: string;
  streamId: string;
  currentTime: number;
}

/**
 * Review-mode sensor card. Dispatches internally:
 *
 * - If the recorded channels contain ``roll``/``pitch``/``yaw`` the
 *   panel shows the 3D pose cube at the current playback time (reuses
 *   the Record-mode :component:`PosePanel`).
 * - Otherwise the panel falls back to a multi-channel line chart with
 *   a playback cursor.
 *
 * Dispatch happens *after* fetching the sensor file because channel
 * names aren't available in the episode manifest; the fetch is shared
 * across dispatch decisions so we don't pay for it twice.
 */
export function ReviewSensorPanel({
  episodeId,
  streamId,
  currentTime,
}: ReviewSensorPanelProps) {
  const { data, isLoading, error } = useSensorReplay(episodeId, streamId);

  const orientationSnapshot = useMemo(() => {
    if (!data || data.t.length === 0) return null;
    const names = Object.keys(data.channels);
    if (
      !names.includes("roll") ||
      !names.includes("pitch") ||
      !names.includes("yaw")
    ) {
      return null;
    }
    const idx = sampleIndexAt(data.t, currentTime);
    const snapshot: Record<string, number[]> = {};
    for (const [name, values] of Object.entries(data.channels)) {
      const v = values[idx];
      snapshot[name] = [typeof v === "number" && !Number.isNaN(v) ? v : 0];
    }
    return snapshot;
  }, [data, currentTime]);

  if (isLoading) {
    return (
      <div className="flex h-full w-full items-center justify-center text-xs text-muted">
        Loading sensor…
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="flex h-full w-full items-center justify-center text-xs text-muted">
        {error ?? "No sensor data"}
      </div>
    );
  }

  if (orientationSnapshot) {
    return <PosePanel channels={orientationSnapshot} variant="fill" />;
  }
  return (
    <ReviewSensorChart
      episodeId={episodeId}
      streamId={streamId}
      currentTime={currentTime}
    />
  );
}
