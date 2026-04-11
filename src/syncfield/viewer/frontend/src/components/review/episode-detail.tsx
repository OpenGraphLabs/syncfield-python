import { useEpisode } from "@/hooks/use-episode";
import { useSync } from "@/hooks/use-sync";
import { usePlayback } from "@/hooks/use-playback";
import { useDriftData } from "@/hooks/use-drift-data";
import { SyncButton } from "./sync-button";
import { SyncQualityPanel } from "./sync-quality-panel";
import { ReviewVideoPlayer } from "./review-video-player";
import { ReviewTimeline } from "./review-timeline";
import { DriftChart } from "./drift-chart";

interface EpisodeDetailProps {
  episodeId: string;
  onBack: () => void;
}

/**
 * Full episode review view — video playback + timeline + drift chart + sync sidebar.
 */
export function EpisodeDetail({ episodeId, onBack }: EpisodeDetailProps) {
  const { episode, isLoading, error, refresh } = useEpisode(episodeId);
  const { triggerSync, jobStatus, isSyncing, error: syncError } = useSync();
  const { driftData, isLoading: driftLoading } = useDriftData(episodeId);
  const playback = usePlayback();

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted">
        Loading episode…
      </div>
    );
  }

  if (error || !episode) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2">
        <p className="text-sm text-destructive">{error ?? "Episode not found"}</p>
        <button
          onClick={onBack}
          className="rounded-lg border px-3 py-1 text-xs font-medium hover:bg-foreground/5"
        >
          ← Back
        </button>
      </div>
    );
  }

  const streams = episode.streams;
  const primaryStream = episode.sync_report?.summary.primary_stream ?? streams[0] ?? "";
  const secondaryStreams = streams.filter((s) => s !== primaryStream);

  function handleSync() {
    triggerSync(episodeId).then(() => refresh());
  }

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center gap-3 border-b px-4 py-2">
        <button
          onClick={onBack}
          className="text-xs text-muted transition-colors hover:text-foreground"
        >
          ← Episodes
        </button>
        <div className="h-4 w-px bg-border" />
        <span className="font-mono text-xs font-medium">{episodeId}</span>
        {episode.has_synced_videos && (
          <span className="rounded-md bg-success/10 px-2 py-0.5 text-[10px] font-medium text-success">
            Synced
          </span>
        )}
        <div className="flex-1" />
        {syncError && (
          <span className="text-[10px] text-destructive">{syncError}</span>
        )}
        <SyncButton
          jobStatus={jobStatus}
          isSyncing={isSyncing}
          hasSyncReport={episode.sync_report !== null}
          onSync={handleSync}
        />
      </div>

      {/* Main content */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left: Videos + Timeline + Drift chart */}
        <div className="flex flex-1 flex-col">
          {/* Video area */}
          <div className="flex flex-1 gap-1 bg-black/95 p-2">
            {/* Primary */}
            {primaryStream && (
              <ReviewVideoPlayer
                episodeId={episodeId}
                streamId={primaryStream}
                isPrimary
                videoRef={playback.videoRef}
              />
            )}
            {/* Secondary videos */}
            {secondaryStreams.map((sid) => {
              const streamResult = episode.sync_report?.streams[sid];
              return (
                <ReviewVideoPlayer
                  key={sid}
                  episodeId={episodeId}
                  streamId={sid}
                  isPrimary={false}
                  syncTime={playback.currentTime}
                  driftMs={streamResult?.offset_ms}
                />
              );
            })}
          </div>

          {/* Timeline */}
          <ReviewTimeline
            currentTime={playback.currentTime}
            duration={playback.duration}
            isPlaying={playback.isPlaying}
            playbackRate={playback.playbackRate}
            onSeek={playback.seek}
            onToggle={playback.toggle}
            onSetRate={playback.setPlaybackRate}
          />

          {/* Drift chart */}
          {(episode.has_synced_videos || driftData) && (
            <div className="border-t">
              <DriftChart data={driftData} isLoading={driftLoading} />
            </div>
          )}
        </div>

        {/* Right sidebar */}
        <div className="w-64 shrink-0 overflow-y-auto border-l">
          <SyncQualityPanel
            report={episode.sync_report}
            streams={streams}
          />
        </div>
      </div>
    </div>
  );
}
