import { useSensorStream } from "@/hooks/use-sensor-stream";
import { PosePanel } from "./pose-panel";
import { Quest3PosePanel } from "./quest3-pose-panel";
import { SensorChart } from "./sensor-chart";

interface SensorPanelProps {
  streamId: string;
  windowSeconds?: number;
  variant?: "aspect" | "fill";
}

/**
 * Dispatcher across the three sensor render paths:
 *
 *  - :component:`Quest3PosePanel` — MetaQuestHandStream samples
 *    (detected by the ``hand_joints`` list channel in ``pose``).
 *  - :component:`PosePanel` — roll/pitch/yaw IMUs (e.g. WitMotion).
 *  - :component:`SensorChart` — fallback multi-channel line chart.
 *
 * The viewer's SSE sensor endpoint is rate-limited server-side to
 * ~10 Hz, so one shared subscription per stream is enough. We open
 * :hook:`useSensorStream` once, look at both the scalar channel names
 * and the pose payload to pick a panel, and that panel opens its own
 * short subscription for rendering.
 *
 * The render decision keys off *data arrival*, not ``isConnected``.
 * Some EventSource implementations / middleware deliver messages
 * without firing ``onopen`` reliably; the data itself is ground
 * truth that the stream is alive.
 */

function hasOrientationChannels(names: string[]): boolean {
  return (
    names.includes("roll") &&
    names.includes("pitch") &&
    names.includes("yaw")
  );
}

function hasQuest3Pose(pose: Record<string, number[]> | null): boolean {
  return Boolean(pose && Array.isArray(pose.hand_joints) && pose.hand_joints.length > 0);
}

export function SensorPanel({
  streamId,
  windowSeconds,
  variant = "aspect",
}: SensorPanelProps) {
  const { channels, pose, isConnected } = useSensorStream(streamId);
  const channelNames = Object.keys(channels);
  const hasScalar = channelNames.length > 0;
  const hasPose = hasQuest3Pose(pose);
  const hasData = hasScalar || hasPose;

  if (hasData) {
    if (hasPose) {
      return <Quest3PosePanel pose={pose} variant={variant} />;
    }
    if (hasOrientationChannels(channelNames)) {
      return <PosePanel channels={channels} variant={variant} />;
    }
    return (
      <SensorChart
        streamId={streamId}
        windowSeconds={windowSeconds}
        variant={variant}
      />
    );
  }

  const wrapperClass =
    variant === "fill" ? "flex h-full w-full" : "flex aspect-video";
  return (
    <div className={`${wrapperClass} items-center justify-center text-xs text-muted`}>
      {isConnected ? "Waiting for data…" : "Connecting…"}
    </div>
  );
}
