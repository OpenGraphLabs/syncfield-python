import { useMemo } from "react";
import { PosePanel } from "@/components/pose-panel";
import { Quest3PosePanel } from "@/components/quest3-pose-panel";
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

  // Quest 3 pose: hand_joints / joint_rotations / head_pose live in
  // ``vector_channels`` (server splits scalar vs vector channels so
  // the line-chart code path doesn't have to handle list samples).
  // Build a per-frame snapshot that matches the live Record-mode
  // pose payload shape, so Quest3PosePanel can be reused as-is.
  const quest3Snapshot = useMemo(() => {
    const vec = data?.vector_channels;
    if (!data || !vec || data.t.length === 0) return null;
    if (!vec.hand_joints || !vec.hand_joints.length) return null;
    const idx = sampleIndexAt(data.t, currentTime);
    const snapshot: Record<string, number[]> = {};
    for (const [name, frames] of Object.entries(vec)) {
      const frame = frames[idx];
      if (Array.isArray(frame) && frame.length > 0) {
        snapshot[name] = frame;
      }
    }
    return Object.keys(snapshot).length > 0 ? snapshot : null;
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

  if (quest3Snapshot) {
    return <Quest3PosePanel pose={quest3Snapshot} />;
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
