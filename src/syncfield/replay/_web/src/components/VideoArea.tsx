import { useEffect, useRef } from "react";
import type { ReplayStream } from "../types";
import type { SyncMode } from "../hooks/useBeforeAfter";
import { computeStreamTime } from "../hooks/useBeforeAfter";

interface Props {
  streams: ReplayStream[];
  mode: SyncMode;
  offsetFor: (streamId: string) => number | undefined;
  masterTime: number;
  isPlaying: boolean;
  seekVersion: number;
  onTimeUpdate?: (time: number) => void;
  onDurationChange?: (duration: number) => void;
}

const DRIFT_TOLERANCE = 0.05; // seconds

export default function VideoArea({
  streams,
  mode,
  offsetFor,
  masterTime,
  isPlaying,
  seekVersion,
  onTimeUpdate,
  onDurationChange,
}: Props) {
  const videoStreams = streams.filter((s) => s.kind === "video" && s.media_url);
  const videoRefs = useRef<Map<string, HTMLVideoElement>>(new Map());

  // Apply seek when version bumps OR mode flips OR offsets shift.
  useEffect(() => {
    for (const s of videoStreams) {
      const el = videoRefs.current.get(s.id);
      if (!el) continue;
      const target = computeStreamTime(masterTime, offsetFor(s.id), mode);
      if (Math.abs(el.currentTime - target) > DRIFT_TOLERANCE) {
        el.currentTime = target;
      }
    }
  }, [seekVersion, mode, masterTime, videoStreams, offsetFor]);

  // Play / pause sync
  useEffect(() => {
    for (const s of videoStreams) {
      const el = videoRefs.current.get(s.id);
      if (!el) continue;
      if (isPlaying) {
        el.play().catch(() => {});
      } else {
        el.pause();
      }
    }
  }, [isPlaying, videoStreams]);

  if (videoStreams.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-zinc-400 text-sm">
        No video streams in this session
      </div>
    );
  }

  // First video drives the master time. Other videos follow via seek effect.
  const heroId = videoStreams[0].id;

  return (
    <div className="grid h-full w-full gap-2 p-2 grid-cols-1 md:grid-cols-2 bg-black">
      {videoStreams.map((s) => (
        <div key={s.id} className="relative bg-black">
          <video
            data-testid="replay-video"
            ref={(el) => {
              if (el) videoRefs.current.set(s.id, el);
              else videoRefs.current.delete(s.id);
            }}
            src={s.media_url ?? undefined}
            preload="auto"
            className="h-full w-full object-contain"
            onTimeUpdate={
              s.id === heroId && onTimeUpdate
                ? (e) => onTimeUpdate(e.currentTarget.currentTime)
                : undefined
            }
            onLoadedMetadata={
              s.id === heroId && onDurationChange
                ? (e) => onDurationChange(e.currentTarget.duration)
                : undefined
            }
          />
          <div className="absolute left-2 top-2 rounded bg-black/60 px-2 py-0.5 font-mono text-[10px] text-white">
            {s.id}
          </div>
        </div>
      ))}
    </div>
  );
}
