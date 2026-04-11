import { useCallback, useEffect, useState } from "react";
import { useSession } from "@/hooks/use-session";
import { useDiscovery } from "@/hooks/use-discovery";
import { Header } from "@/components/header";
import { ControlPanel } from "@/components/control-panel";
import { SessionClock } from "@/components/session-clock";
import { StreamCard } from "@/components/stream-card";
import { HealthTable } from "@/components/health-table";
import { CountdownOverlay } from "@/components/countdown-overlay";
import { DiscoveryModal } from "@/components/discovery-modal";
import { Footer } from "@/components/footer";

// ---------------------------------------------------------------------------
// App
//
// Audio feedback (countdown ticks + chirps) is handled entirely by the
// recording PC via sounddevice/PortAudio. The browser only shows the
// visual countdown overlay — no Web Audio playback.
// ---------------------------------------------------------------------------

export function App() {
  const { snapshot, countdown, sendCommand, connectionStatus } = useSession();
  const discovery = useDiscovery();
  const [discoveryOpen, setDiscoveryOpen] = useState(false);

  // Update page title with session state
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
  const canRemove = state === "idle" || state === "connected" || state === "stopped";

  return (
    <div className="flex h-screen flex-col">
      {/* Header */}
      <Header
        snapshot={snapshot}
        onDiscoverClick={() => setDiscoveryOpen(true)}
      />

      {/* Control + Session clock */}
      <ControlPanel state={state} onCommand={sendCommand} />
      <SessionClock snapshot={snapshot} />

      {/* Main content area */}
      <div className="flex flex-1 overflow-hidden">
        {/* Streams section (main) */}
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

        {/* Health events sidebar */}
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

      {/* Footer */}
      <Footer outputDir={snapshot?.output_dir ?? ""} />

      {/* Connection status indicator */}
      {connectionStatus === "disconnected" && (
        <div className="fixed bottom-10 left-1/2 -translate-x-1/2 rounded-full bg-destructive px-4 py-1.5 text-xs font-medium text-white shadow-lg">
          Reconnecting…
        </div>
      )}

      {/* Countdown overlay */}
      {countdown !== null && <CountdownOverlay count={countdown} />}

      {/* Discovery modal */}
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
