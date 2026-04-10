import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import VideoArea from "./components/VideoArea";
import SyncReportPanel from "./components/SyncReportPanel";
import BeforeAfterToggle from "./components/BeforeAfterToggle";
import SensorChartPanel from "./components/SensorChartPanel";
import { useReplaySession } from "./hooks/useReplaySession";
import { useBeforeAfter } from "./hooks/useBeforeAfter";
import { useSensorData } from "./hooks/useSensorData";

function formatTime(t: number): string {
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  const ms = Math.floor((t % 1) * 1000);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(ms).padStart(3, "0")}`;
}

export default function App() {
  const { session, syncReport, loading, error } = useReplaySession();
  const beforeAfter = useBeforeAfter(syncReport);
  const sensors = useSensorData(session?.streams ?? []);

  const [masterTime, setMasterTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [seekVersion, setSeekVersion] = useState(0);
  const seekTargetTimeRef = useRef(0);

  const seekTo = useCallback(
    (t: number) => {
      const clamped = Math.max(0, Math.min(t, duration || t));
      seekTargetTimeRef.current = clamped;
      setMasterTime(clamped);
      setSeekVersion((v) => v + 1);
    },
    [duration],
  );

  const togglePlay = useCallback(() => setIsPlaying((p) => !p), []);

  // Keyboard: Space play, ← / → 5s seek
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      switch (e.code) {
        case "Space":
          e.preventDefault();
          togglePlay();
          break;
        case "ArrowLeft":
          e.preventDefault();
          seekTo(masterTime - 5);
          break;
        case "ArrowRight":
          e.preventDefault();
          seekTo(masterTime + 5);
          break;
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [masterTime, seekTo, togglePlay]);

  const hasSensors = sensors.length > 0;

  const sessionContent = useMemo(() => {
    if (loading) {
      return (
        <div className="flex h-full items-center justify-center text-zinc-400 text-sm">
          Loading session…
        </div>
      );
    }
    if (error || !session) {
      return (
        <div className="flex h-full items-center justify-center text-red-500 text-sm">
          {error ?? "Session not found"}
        </div>
      );
    }
    return (
      <div className="flex flex-col h-full">
        {/* Header */}
        <div className="flex items-center gap-4 border-b border-zinc-200/60 bg-white px-5 py-3">
          <div>
            <div className="text-sm font-semibold text-zinc-800">SyncField Replay</div>
            <div className="font-mono text-[11px] text-zinc-500">{session.host_id}</div>
          </div>
          <div className="flex-1 flex justify-center">
            <BeforeAfterToggle
              mode={beforeAfter.mode}
              disabled={!beforeAfter.hasReport}
              onChange={beforeAfter.setMode}
            />
          </div>
          <div className="min-w-[260px]">
            <SyncReportPanel report={syncReport} />
          </div>
        </div>

        {/* Body: video left, sensors right */}
        <div className="flex flex-1 min-h-0">
          <div className="flex-[7] min-w-0">
            <VideoArea
              streams={session.streams}
              mode={beforeAfter.mode}
              offsetFor={beforeAfter.offsetFor}
              masterTime={masterTime}
              isPlaying={isPlaying}
              seekVersion={seekVersion}
              onTimeUpdate={(t) => {
                // ignore small updates while we're actively seeking
                if (Math.abs(t - seekTargetTimeRef.current) < 0.5) {
                  setMasterTime(t);
                }
              }}
              onDurationChange={setDuration}
            />
          </div>
          {hasSensors && (
            <div className="flex-[3] min-w-[300px] max-w-[480px] border-l border-zinc-200/60 bg-white">
              <SensorChartPanel
                sensors={sensors}
                masterTime={masterTime}
                duration={duration}
              />
            </div>
          )}
        </div>

        {/* Playback controls */}
        <div className="flex items-center gap-4 border-t border-zinc-200/60 bg-white px-4 py-2">
          <button
            type="button"
            onClick={togglePlay}
            className="rounded-md px-3 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-100"
          >
            {isPlaying ? "Pause" : "Play"}
          </button>
          <span className="font-mono text-xs text-zinc-500 tabular-nums">
            {formatTime(masterTime)} / {formatTime(duration)}
          </span>
          <input
            type="range"
            min={0}
            max={duration || 0}
            step={0.01}
            value={masterTime}
            onChange={(e) => seekTo(parseFloat(e.target.value))}
            className="flex-1 accent-zinc-700"
          />
        </div>
      </div>
    );
  }, [
    loading,
    error,
    session,
    syncReport,
    beforeAfter,
    masterTime,
    duration,
    isPlaying,
    seekVersion,
    seekTo,
    togglePlay,
    sensors,
    hasSensors,
  ]);

  return (
    <div className="h-full bg-[#FAF8F6] text-zinc-800">{sessionContent}</div>
  );
}
