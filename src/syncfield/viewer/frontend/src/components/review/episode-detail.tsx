import { useCallback, useEffect, useRef, useState } from "react";
import { useEpisode } from "@/hooks/use-episode";
import { useSync } from "@/hooks/use-sync";
import { usePlayback } from "@/hooks/use-playback";
import { useDriftData } from "@/hooks/use-drift-data";
import { SyncButton } from "./sync-button";
import { SyncQualityPanel } from "./sync-quality-panel";
import { ReviewVideoPlayer } from "./review-video-player";
import { ReviewTimeline } from "./review-timeline";
import { DriftChart } from "./drift-chart";
import { SyncComparisonModal } from "./sync-comparison-modal";

interface EpisodeDetailProps {
  episodeId: string;
  onBack: () => void;
}

export function EpisodeDetail({ episodeId, onBack }: EpisodeDetailProps) {
  const { episode, isLoading, error, refresh } = useEpisode(episodeId);
  const { triggerSync, jobStatus, isSyncing, error: syncError } = useSync();
  const { driftData, isLoading: driftLoading } = useDriftData(episodeId);
  const playback = usePlayback();

  // Sync comparison modal state
  const [compareStream, setCompareStream] = useState<string | null>(null);

  // Auto-refresh episode data when sync completes
  const prevSyncing = useRef(false);
  useEffect(() => {
    if (prevSyncing.current && !isSyncing && jobStatus?.status === "complete") {
      refresh();
    }
    prevSyncing.current = isSyncing;
  }, [isSyncing, jobStatus, refresh]);

  const handleStreamClick = useCallback((streamId: string) => {
    setCompareStream(streamId);
  }, []);

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
        <p className="text-sm text-destructive">
          {error ?? "Episode not found"}
        </p>
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
  const syncReport = episode.sync_report;
  const primaryStream =
    syncReport?.summary.primary_stream ?? streams[0] ?? "";
  const secondaryStreams = streams.filter((s) => s !== primaryStream);
  const fps = syncReport?.summary.actual_mean_fps ?? 30;

  // Get drift for the compare target
  const compareResult = compareStream
    ? syncReport?.streams[compareStream]
    : null;
  const compareDriftMs = compareResult?.offset_ms ?? 0;

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
        <SyncButton
          jobStatus={jobStatus}
          isSyncing={isSyncing}
          hasSyncReport={syncReport !== null}
          onSync={() => triggerSync(episodeId)}
        />
      </div>

      {/* Sync error banner */}
      {syncError && (
        <div className="border-b border-destructive/20 bg-destructive/5 px-4 py-3">
          <p className="text-xs font-medium text-destructive">
            Sync failed: {syncError}
          </p>
          <p className="mt-1 text-[11px] text-muted">
            Make sure the SyncField container is running locally (
            <code className="rounded bg-foreground/5 px-1 py-0.5 font-mono text-[10px]">
              docker compose up
            </code>
            ), or configure a remote endpoint via{" "}
            <code className="rounded bg-foreground/5 px-1 py-0.5 font-mono text-[10px]">
              viewer.launch(session, sync_endpoint="https://...")
            </code>
          </p>
        </div>
      )}

      {/* Main content */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left: Videos + Timeline + Drift chart */}
        <div className="flex flex-1 flex-col">
          {/* Video area */}
          <div className="flex flex-1 gap-1 bg-black/95 p-2">
            {primaryStream && (
              <ReviewVideoPlayer
                episodeId={episodeId}
                streamId={primaryStream}
                isPrimary
                videoRef={playback.videoRef}
              />
            )}
            {secondaryStreams.map((sid) => {
              const streamResult = syncReport?.streams[sid];
              return (
                <ReviewVideoPlayer
                  key={sid}
                  episodeId={episodeId}
                  streamId={sid}
                  isPrimary={false}
                  syncTime={playback.currentTime}
                  isPlaying={playback.isPlaying}
                  driftMs={streamResult?.offset_ms}
                  onClick={
                    syncReport ? () => handleStreamClick(sid) : undefined
                  }
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
        </div>

        {/* Right sidebar */}
        <div className="w-72 shrink-0 overflow-y-auto border-l">
          <SyncQualityPanel
            report={syncReport}
            streams={streams}
            primaryStream={primaryStream}
            onStreamClick={syncReport ? handleStreamClick : undefined}
          />

          {/* Drift chart in sidebar */}
          {(episode.has_synced_videos || driftData) && (
            <div className="border-t">
              <DriftChart data={driftData} isLoading={driftLoading} />
            </div>
          )}
        </div>
      </div>

      {/* Sync comparison modal */}
      {compareStream && (
        <SyncComparisonModal
          streamId={compareStream}
          primaryStreamId={primaryStream}
          episodeId={episodeId}
          driftMs={compareDriftMs}
          fps={fps}
          currentTime={playback.currentTime}
          onClose={() => setCompareStream(null)}
        />
      )}
    </div>
  );
}
