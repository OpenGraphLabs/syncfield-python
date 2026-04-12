import { useCallback, useEffect, useRef, useState } from "react";
import type { SyncJobStatus } from "@/lib/review-types";

const POLL_INTERVAL_MS = 3000;

interface UseSyncReturn {
  /** Trigger a sync job for the given episode. */
  triggerSync: (episodeId: string) => Promise<void>;
  /** Current sync job status from polling, if any. */
  jobStatus: SyncJobStatus | null;
  /** Whether a sync job is currently in progress. */
  isSyncing: boolean;
  /** Error message from the last failed trigger or poll, if any. */
  error: string | null;
  /** Stop polling the current sync job. */
  cancel: () => void;
}

/**
 * REST hook for triggering and monitoring sync jobs.
 *
 * `triggerSync(episodeId)` POSTs to `/api/episodes/{id}/sync` then
 * polls `GET /api/episodes/{id}/sync-status/{job_id}` every 3 seconds
 * until the job reaches "complete" or "failed". Call `cancel()` to
 * stop polling early. Cleans up the interval on unmount.
 */
export function useSync(): UseSyncReturn {
  const [jobStatus, setJobStatus] = useState<SyncJobStatus | null>(null);
  const [isSyncing, setIsSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const mountedRef = useRef(true);

  const stopPolling = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  const cancel = useCallback(() => {
    stopPolling();
    setIsSyncing(false);
  }, [stopPolling]);

  // Cleanup on unmount
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      stopPolling();
    };
  }, [stopPolling]);

  const triggerSync = useCallback(
    async (episodeId: string) => {
      // Reset state
      stopPolling();
      setJobStatus(null);
      setError(null);
      setIsSyncing(true);

      try {
        const res = await fetch(`/api/episodes/${episodeId}/sync`, {
          method: "POST",
        });
        if (!res.ok) {
          const body = await res.json().catch(() => null);
          const msg = body?.error ?? `Failed to trigger sync (${res.status})`;
          throw new Error(msg);
        }
        const data: SyncJobStatus = await res.json();
        if (!mountedRef.current) return;
        setJobStatus(data);

        // If already terminal, we're done
        if (data.status === "complete" || data.status === "failed") {
          setIsSyncing(false);
          return;
        }

        // Start polling
        const jobId = data.job_id;
        intervalRef.current = setInterval(async () => {
          try {
            const pollRes = await fetch(
              `/api/episodes/${episodeId}/sync-status/${jobId}`,
            );
            if (!pollRes.ok) {
              throw new Error(`Poll failed (${pollRes.status})`);
            }
            const pollData: SyncJobStatus = await pollRes.json();
            if (!mountedRef.current) return;
            setJobStatus(pollData);

            if (
              pollData.status === "complete" ||
              pollData.status === "failed"
            ) {
              stopPolling();
              setIsSyncing(false);
            }
          } catch (pollErr) {
            if (!mountedRef.current) return;
            setError(
              pollErr instanceof Error ? pollErr.message : "Polling failed",
            );
            stopPolling();
            setIsSyncing(false);
          }
        }, POLL_INTERVAL_MS);
      } catch (err) {
        if (!mountedRef.current) return;
        setError(
          err instanceof Error ? err.message : "Failed to trigger sync",
        );
        setIsSyncing(false);
      }
    },
    [stopPolling],
  );

  return { triggerSync, jobStatus, isSyncing, error, cancel };
}
