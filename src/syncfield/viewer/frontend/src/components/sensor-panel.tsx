import { useSensorStream } from "@/hooks/use-sensor-stream";
import { PosePanel } from "./pose-panel";
import { SensorChart } from "./sensor-chart";

interface SensorPanelProps {
  streamId: string;
}

/**
 * Dispatcher between :component:`PosePanel` and :component:`SensorChart`.
 *
 * The viewer's SSE sensor endpoint is already rate-limited server-side
 * to ~10 Hz, so a single shared subscription is both the simplest and
 * most efficient design — no separate "probe" connection needed. This
 * component opens :hook:`useSensorStream` once, decides which panel to
 * render from the first event's channel names, and the chosen panel
 * opens its own short subscription for rendering.
 *
 * Orientation-capable sensors (those that emit ``roll``, ``pitch``,
 * and ``yaw`` channels — e.g. WitMotion WT901BLE) get the 3D cube
 * view; everything else falls back to the generic multi-channel line
 * chart so existing adapters keep working unchanged.
 */

function hasOrientationChannels(names: string[]): boolean {
  return (
    names.includes("roll") &&
    names.includes("pitch") &&
    names.includes("yaw")
  );
}

export function SensorPanel({ streamId }: SensorPanelProps) {
  const { channels, isConnected } = useSensorStream(streamId);
  const channelNames = Object.keys(channels);

  if (!isConnected) {
    return (
      <div className="flex aspect-video items-center justify-center text-xs text-muted">
        Connecting…
      </div>
    );
  }

  if (channelNames.length === 0) {
    return (
      <div className="flex aspect-video items-center justify-center text-xs text-muted">
        Waiting for data…
      </div>
    );
  }

  if (hasOrientationChannels(channelNames)) {
    return <PosePanel channels={channels} />;
  }
  return <SensorChart streamId={streamId} />;
}
