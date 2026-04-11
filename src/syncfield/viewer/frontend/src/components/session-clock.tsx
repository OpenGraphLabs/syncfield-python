import type { SessionSnapshot } from "@/lib/types";
import { formatChirpPair } from "@/lib/format";

interface SessionClockProps {
  snapshot: SessionSnapshot | null;
}

/**
 * Session clock panel — shows sync point, chirp status, and tone config.
 */
export function SessionClock({ snapshot }: SessionClockProps) {
  if (!snapshot) return null;

  const { chirp } = snapshot;
  const chirpLabel = chirp.enabled
    ? formatChirpPair(chirp.start_ns, chirp.stop_ns)
    : "disabled";

  return (
    <div className="flex items-center gap-6 border-b px-4 py-2 text-xs">
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

      {/* Stream count */}
      <div className="flex items-center gap-2">
        <span className="text-muted">Streams</span>
        <span className="font-mono">
          {Object.keys(snapshot.streams).length}
        </span>
      </div>

      {/* Session state detail */}
      <div className="flex items-center gap-2">
        <span className="text-muted">State</span>
        <span className="font-mono">{snapshot.state}</span>
      </div>
    </div>
  );
}
