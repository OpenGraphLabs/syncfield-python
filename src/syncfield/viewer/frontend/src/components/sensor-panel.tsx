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
  const hasData = channelNames.length > 0;

  // If channel data has arrived, render it regardless of isConnected.
  // (Some EventSource implementations or middleware can deliver
  // messages without firing onopen reliably; the data itself is the
  // ground truth that the stream is alive.)
  if (hasData) {
    if (hasOrientationChannels(channelNames)) {
      return <PosePanel channels={channels} />;
    }
    return <SensorChart streamId={streamId} />;
  }

  return (
    <div className="flex aspect-video items-center justify-center text-xs text-muted">
      {isConnected ? "Waiting for data…" : "Connecting…"}
    </div>
  );
}
