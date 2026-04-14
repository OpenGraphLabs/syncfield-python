import { ReviewVideoPlayer } from "./review-video-player";
import { ReviewAudioPanel } from "./review-audio-panel";
import { ReviewSensorPanel } from "./review-sensor-panel";

interface ReviewStreamCellProps {
  episodeId: string;
  streamId: string;
  /** Manifest-declared stream kind. Unknown kinds render as placeholder. */
  kind: string;

  // Video-specific wiring (ignored for non-video kinds).
  isPrimary?: boolean;
  videoRef?: (el: HTMLVideoElement | null) => void;
  syncTime?: number;
  isPlaying?: boolean;
  driftMs?: number;
  onClick?: () => void;

  // Playback time from the primary video. Drives audio + sensor
  // cursors / pose lookups. Ignored for video cells which drive or
  // follow playback through the <video> element itself.
  currentTime: number;
}

/**
 * Unified per-stream cell for Review mode. Dispatches by manifest
 * kind so adding a new modality is a one-line change here rather than
 * a rewrite of :component:`EpisodeDetail`'s layout.
 *
 * Known kinds:
 *   video   → :component:`ReviewVideoPlayer`
 *   audio   → :component:`ReviewAudioPanel`
 *   sensor  → :component:`ReviewSensorPanel` (pose cube for IMUs,
 *                                              line chart otherwise)
 *
 * Unknown / custom kinds fall through to a muted placeholder instead
 * of silently breaking the grid.
 */
export function ReviewStreamCell(props: ReviewStreamCellProps) {
  const { episodeId, streamId, kind, currentTime } = props;

  if (kind === "video") {
    return (
      <ReviewVideoPlayer
        episodeId={episodeId}
        streamId={streamId}
        isPrimary={props.isPrimary ?? false}
        videoRef={props.videoRef}
        syncTime={props.syncTime}
        isPlaying={props.isPlaying}
        driftMs={props.driftMs}
        onClick={props.onClick}
      />
    );
  }

  if (kind === "audio") {
    return (
      <LabeledCard streamId={streamId} kindBadge="audio">
        <ReviewAudioPanel
          episodeId={episodeId}
          streamId={streamId}
          currentTime={currentTime}
        />
      </LabeledCard>
    );
  }

  if (kind === "sensor") {
    return (
      <LabeledCard streamId={streamId} kindBadge="sensor">
        <ReviewSensorPanel
          episodeId={episodeId}
          streamId={streamId}
          currentTime={currentTime}
        />
      </LabeledCard>
    );
  }

  return (
    <LabeledCard streamId={streamId} kindBadge={kind || "custom"}>
      <div className="flex aspect-video items-center justify-center text-[11px] text-muted">
        No preview
      </div>
    </LabeledCard>
  );
}

/**
 * Shared chrome for non-video stream cells — thin border, label in the
 * top-left, muted kind chip. Gives audio / sensor cells the same
 * silhouette as the :component:`ReviewVideoPlayer` (which already
 * draws its own label on top of the MP4).
 */
function LabeledCard({
  streamId,
  kindBadge,
  children,
}: {
  streamId: string;
  kindBadge: string;
  children: React.ReactNode;
}) {
  return (
    <div className="relative flex-1 overflow-hidden rounded-lg border bg-card">
      <div className="pointer-events-none absolute left-2 top-2 z-10 flex items-center gap-1.5">
        <span className="rounded bg-foreground/80 px-1.5 py-0.5 text-[10px] font-medium text-background">
          {streamId}
        </span>
        <span className="rounded bg-foreground/10 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider text-muted">
          {kindBadge}
        </span>
      </div>
      <div className="flex h-full items-center justify-center">{children}</div>
    </div>
  );
}
