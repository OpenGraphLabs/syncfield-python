import { useCallback, useEffect, useState } from "react";
import { useSession } from "@/hooks/use-session";
import { useDiscovery } from "@/hooks/use-discovery";
import { cn } from "@/lib/utils";
import { Header } from "@/components/header";
import { ControlPanel } from "@/components/control-panel";
import { SessionClock } from "@/components/session-clock";
import { StreamCard } from "@/components/stream-card";
import { HealthTable } from "@/components/health-table";
import { CountdownOverlay } from "@/components/countdown-overlay";
import { DiscoveryModal } from "@/components/discovery-modal";
import { Footer } from "@/components/footer";
import { ReviewPage } from "@/components/review/review-page";
import type { ViewMode } from "@/components/segment-control";

// ---------------------------------------------------------------------------
// App
//
// Two modes: Record (live session monitoring) and Review (episode browsing
// + sync analysis). Switched via the header segment control.
// ---------------------------------------------------------------------------

export function App() {
  const [mode, setMode] = useState<ViewMode>("record");

  return mode === "record" ? (
    <RecordView mode={mode} onModeChange={setMode} />
  ) : (
    <ReviewView mode={mode} onModeChange={setMode} />
  );
}

// ---------------------------------------------------------------------------
// Record view (existing functionality)
// ---------------------------------------------------------------------------

function RecordView({
  mode,
  onModeChange,
}: {
  mode: ViewMode;
  onModeChange: (m: ViewMode) => void;
}) {
  const { snapshot, countdown, sendCommand } = useSession();
  const discovery = useDiscovery();
  const [discoveryOpen, setDiscoveryOpen] = useState(false);

  useEffect(() => {
    const state = snapshot?.state ?? "idle";
    document.title = state === "recording" ? "● SyncField" : "SyncField";
  }, [snapshot?.state]);

  const handleRemoveStream = useCallback(
    (streamId: string) => {
      discovery.removeStream(streamId);
    },
    [discovery],
  );

  const state = snapshot?.state ?? "idle";
  const streams = snapshot?.streams ?? {};
  const streamList = Object.values(streams);
  const canRemove =
    state === "idle" || state === "connected" || state === "stopped";
  const isRecording = state === "recording";

  return (
    <div
      className={cn(
        "flex h-screen flex-col transition-shadow",
        isRecording && "shadow-[inset_0_0_0_3px_hsl(0_65%_48%)]",
      )}
    >
      <Header
        snapshot={snapshot}
        onDiscoverClick={() => setDiscoveryOpen(true)}
        mode={mode}
        onModeChange={onModeChange}
      />

      <ControlPanel state={state} onCommand={sendCommand} />
      <SessionClock snapshot={snapshot} />

      <div className="flex flex-1 overflow-hidden">
        <div className="flex-1 overflow-y-auto p-4">
          {streamList.length > 0 ? (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {streamList.map((stream) => (
                <StreamCard
                  key={stream.id}
                  stream={stream}
                  canRemove={canRemove}
                  onRemove={handleRemoveStream}
                />
              ))}
            </div>
          ) : (
            <div className="flex h-full flex-col items-center justify-center gap-3">
              <p className="text-sm text-muted">No streams registered</p>
              <button
                onClick={() => setDiscoveryOpen(true)}
                className="rounded-lg bg-primary px-4 py-2 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
              >
                Discover Devices
              </button>
            </div>
          )}
        </div>

        {streamList.length > 0 && (
          <div className="hidden w-72 shrink-0 border-l lg:block">
            <div className="px-3 py-2.5">
              <h3 className="text-xs font-medium text-muted">Health Events</h3>
            </div>
            <div className="overflow-y-auto">
              <HealthTable entries={snapshot?.health_log ?? []} />
            </div>
          </div>
        )}
      </div>

      <Footer outputDir={snapshot?.output_dir ?? ""} />

      {countdown !== null && <CountdownOverlay count={countdown} />}

      <DiscoveryModal
        isOpen={discoveryOpen}
        onClose={() => setDiscoveryOpen(false)}
        devices={discovery.devices}
        isScanning={discovery.isScanning}
        error={discovery.error}
        onScan={discovery.scan}
        onAdd={discovery.addDevice}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Review view
// ---------------------------------------------------------------------------

function ReviewView({
  mode,
  onModeChange,
}: {
  mode: ViewMode;
  onModeChange: (m: ViewMode) => void;
}) {
  return (
    <div className="flex h-screen flex-col">
      <Header
        snapshot={null}
        onDiscoverClick={() => {}}
        mode={mode}
        onModeChange={onModeChange}
        showRecordingControls={false}
      />
      <ReviewPage />
    </div>
  );
}
