import { useEffect, useState } from "react";
import type { SessionManifest, SyncReport } from "../types";

export interface ReplaySessionState {
  session: SessionManifest | null;
  syncReport: SyncReport | null;
  loading: boolean;
  error: string | null;
}

export function useReplaySession(): ReplaySessionState {
  const [session, setSession] = useState<SessionManifest | null>(null);
  const [syncReport, setSyncReport] = useState<SyncReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [s, r] = await Promise.all([
          fetch("/api/session"),
          fetch("/api/sync-report"),
        ]);
        if (!s.ok) throw new Error(`session fetch failed: ${s.status}`);
        const sessionJson = (await s.json()) as SessionManifest;
        const reportJson = r.ok ? ((await r.json()) as SyncReport) : null;
        if (!cancelled) {
          setSession(sessionJson);
          setSyncReport(reportJson);
        }
      } catch (err) {
        if (!cancelled) setError(String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return { session, syncReport, loading, error };
}
