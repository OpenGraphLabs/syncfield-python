import { useCallback, useEffect, useRef, useState } from "react";
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
// Audio feedback — countdown tick (C6, 1047 Hz, 100 ms)
// ---------------------------------------------------------------------------

function playCountdownTick() {
  try {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = 1047; // C6
    gain.gain.value = 0.3;
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.1);
    osc.stop(ctx.currentTime + 0.1);
    // Clean up after playback
    setTimeout(() => ctx.close(), 200);
  } catch {
    // Audio not available — silent fallback
  }
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

export function App() {
  const { snapshot, countdown, sendCommand, connectionStatus } = useSession();
  const discovery = useDiscovery();
  const [discoveryOpen, setDiscoveryOpen] = useState(false);

  // Play tick sound on countdown events
  const lastCountdown = useRef<number | null>(null);
  useEffect(() => {
    if (countdown !== null && countdown !== lastCountdown.current) {
      playCountdownTick();
    }
    lastCountdown.current = countdown;
  }, [countdown]);

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

      {/* Streams section */}
      <div className="flex-1 overflow-hidden">
        {streamList.length > 0 ? (
          <div className="h-full overflow-y-auto">
            {/* Stream cards — horizontal scroll */}
            <div className="border-b p-4">
              <div className="flex gap-3 overflow-x-auto pb-2">
                {streamList.map((stream) => (
                  <StreamCard
                    key={stream.id}
                    stream={stream}
                    canRemove={canRemove}
                    onRemove={handleRemoveStream}
                  />
                ))}
              </div>
            </div>

            {/* Health table */}
            <div className="p-4">
              <h3 className="mb-2 text-xs font-medium text-muted">
                Health Events
              </h3>
              <div className="rounded-xl border bg-card">
                <HealthTable entries={snapshot?.health_log ?? []} />
              </div>
            </div>
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
