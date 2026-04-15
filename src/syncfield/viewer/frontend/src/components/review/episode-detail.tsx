import { useCallback, useEffect, useRef, useState } from "react";
import { useEpisode } from "@/hooks/use-episode";
import { useSync } from "@/hooks/use-sync";
import { usePlayback } from "@/hooks/use-playback";
import { useDriftData } from "@/hooks/use-drift-data";
import { SyncButton } from "./sync-button";
import { SyncQualityPanel } from "./sync-quality-panel";
import { ReviewVideoPlayer } from "./review-video-player";
import { ReviewStreamCell } from "./review-stream-cell";
import { ReviewTimeline } from "./review-timeline";
import { DriftChart } from "./drift-chart";
import { SyncComparisonModal } from "./sync-comparison-modal";

interface EpisodeDetailProps {
  episodeId: string;
  onBack: () => void;
}

export function EpisodeDetail({ episodeId, onBack }: EpisodeDetailProps) {
  // Increment key to force full re-mount after sync completes
  const [refreshKey, setRefreshKey] = useState(0);

  return (
    <EpisodeDetailInner
      key={`${episodeId}-${refreshKey}`}
      episodeId={episodeId}
      onBack={onBack}
      onSyncComplete={() => setRefreshKey((k) => k + 1)}
    />
  );
}

function EpisodeDetailInner({
  episodeId,
  onBack,
  onSyncComplete,
}: EpisodeDetailProps & { onSyncComplete: () => void }) {
  const { episode, isLoading, error } = useEpisode(episodeId);
  const { triggerSync, jobStatus, isSyncing, error: syncError } = useSync();
  const { driftData, isLoading: driftLoading } = useDriftData(episodeId);
  const playback = usePlayback();

  const [compareStream, setCompareStream] = useState<string | null>(null);

  // When sync completes, trigger full re-mount via parent key change
  const prevSyncing = useRef(false);
  useEffect(() => {
    if (prevSyncing.current && !isSyncing && jobStatus?.status === "complete") {
      onSyncComplete();
    }
    prevSyncing.current = isSyncing;
  }, [isSyncing, jobStatus, onSyncComplete]);

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
  const fps = syncReport?.summary.actual_mean_fps ?? 30;

  // Build a map of stream id → manifest kind for the ReviewStreamCell
  // dispatcher. Missing entries default to "video" to stay compatible
  // with older manifests.
  const kindOf = (sid: string): string =>
    episode.manifest?.streams[sid]?.kind ?? "video";

  // Primary = explicit sync-report choice if available, otherwise the
  // first *video* stream (Review's big tile is meant to play back an
  // MP4). Falls back to the first stream overall if no video exists.
  const firstVideoStream = streams.find((s) => kindOf(s) === "video");
  const primaryStream =
    syncReport?.summary.primary_stream ??
    firstVideoStream ??
    streams[0] ??
    "";
  const secondaryStreams = streams.filter((s) => s !== primaryStream);

  // Get drift for the compare target
  const compareResult = compareStream
    ? syncReport?.streams[compareStream]
    : null;
  const compareDriftMs = compareResult?.offset_ms ?? 0;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Header */}
      <div className="flex shrink-0 items-center gap-3 border-b px-4 py-2">
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
          <p className="text-xs font-medium text-destructive">{syncError}</p>
          {syncError.includes("502") && (
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
          )}
        </div>
      )}

      {/* Main content */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Left: stream panels + timeline. The left column is a strict
            flex-column with ``min-h-0``: primary + secondary shrink to
            fit whatever vertical budget remains after the header and
            the (always-visible) timeline are laid out. The timeline
            carries ``shrink-0`` so it's *never* the thing that gets
            clipped when the viewport is short — the user must always
            be able to reach the play/scrub controls without scrolling. */}
        <div className="flex min-h-0 flex-1 flex-col bg-background-subtle">
          {primaryStream && (
            // Primary stream: 60 % of the vertical budget (was 67 %).
            // Trims the headline panel just enough that the secondary
            // grid below has room to render IMU cubes / pose at a
            // legible size — primary still dominates, but the layout
            // reads as balanced rather than dwarfed-secondaries.
            <div className="flex min-h-0 flex-[3] p-2">
              {kindOf(primaryStream) === "video" ? (
                <ReviewVideoPlayer
                  episodeId={episodeId}
                  streamId={primaryStream}
                  isPrimary
                  videoRef={playback.videoRef}
                />
              ) : (
                <ReviewStreamCell
                  episodeId={episodeId}
                  streamId={primaryStream}
                  kind={kindOf(primaryStream)}
                  currentTime={playback.currentTime}
                  isPrimary
                />
              )}
            </div>
          )}

          {secondaryStreams.length > 0 && (
            // Secondary grid: 40 % of vertical (was 33 %), wider
            // minmax so each card renders at a useful size.
            <div className="grid min-h-0 flex-[2] auto-rows-fr gap-2 p-2 grid-cols-[repeat(auto-fit,minmax(260px,1fr))]">
              {secondaryStreams.map((sid) => {
                const kind = kindOf(sid);
                const streamResult = syncReport?.streams[sid];
                return (
                  <div key={sid} className="flex min-h-0 min-w-0">
                    <ReviewStreamCell
                      episodeId={episodeId}
                      streamId={sid}
                      kind={kind}
                      currentTime={playback.currentTime}
                      syncTime={
                        kind === "video" ? playback.currentTime : undefined
                      }
                      isPlaying={
                        kind === "video" ? playback.isPlaying : undefined
                      }
                      driftMs={streamResult?.offset_ms}
                      onClick={
                        kind === "video" && syncReport
                          ? () => handleStreamClick(sid)
                          : undefined
                      }
                    />
                  </div>
                );
              })}
            </div>
          )}

          <div className="shrink-0">
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
