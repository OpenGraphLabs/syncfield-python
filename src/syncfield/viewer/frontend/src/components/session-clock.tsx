import type { SessionSnapshot } from "@/lib/types";
import { formatChirpPair } from "@/lib/format";
import { ChirpModeSelector } from "./chirp-mode-selector";

interface SessionClockProps {
  snapshot: SessionSnapshot | null;
}

/**
 * Session info bar — shows chirp status, the chirp-mode selector, and
 * stream count.
 *
 * The chirp-mode selector is gated whenever the session is in a state
 * that does not accept reconfiguration (anything other than idle /
 * connected / stopped); the SDK rejects with HTTP 409 in those
 * windows, but the disabled state is the friendly UI hint.
 */
export function SessionClock({ snapshot }: SessionClockProps) {
  if (!snapshot) return null;

  const { chirp } = snapshot;
  const chirpLabel = chirp.enabled
    ? formatChirpPair(chirp.start_ns, chirp.stop_ns)
    : "disabled";
  // Chirp mode is only reconfigurable in the "before/after a session"
  // window — idle (not connected) or stopped (post-recording, devices
  // about to be torn down). Connected / connecting / preparing /
  // countdown / recording / stopping all gray out the selector so the
  // UI matches the SDK contract documented at
  // SessionOrchestrator.set_chirp_mode. To switch mode while connected
  // the user must Disconnect first.
  const reconfigurable =
    snapshot.state === "idle" || snapshot.state === "stopped";

  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b px-4 py-2 text-xs">
      {/* Chirp status */}
      <div className="flex items-center gap-2">
        <span className="text-muted">Chirp</span>
        <span className="font-mono">
          {chirp.enabled ? (
            <span className="text-success">{chirpLabel}</span>
          ) : (
            <span className="text-muted">disabled</span>
          )}
        </span>
      </div>

      {/* Chirp mode selector */}
      <div className="flex items-center gap-2">
        <span className="text-muted">Mode</span>
        <ChirpModeSelector value={chirp.mode} disabled={!reconfigurable} />
      </div>

      {/* Stream count */}
      <div className="flex items-center gap-2">
        <span className="text-muted">Streams</span>
        <span className="font-mono">
          {Object.keys(snapshot.streams).length}
        </span>
      </div>
    </div>
  );
}
