import { useCallback, useEffect, useState } from "react";
import type { EpisodeSummary } from "@/lib/review-types";

interface UseEpisodesReturn {
  /** List of episodes from the most recent fetch. */
  episodes: EpisodeSummary[];
  /** Whether the episode list is being loaded. */
  isLoading: boolean;
  /** Error message from the last failed fetch, if any. */
  error: string | null;
  /** Re-fetch the episode list. */
  refresh: () => Promise<void>;
}

/**
 * REST hook for the episode list.
 *
 * Fetches `GET /api/episodes` on mount and exposes a `refresh()`
 * callback for manual re-fetching.
 */
export function useEpisodes(): UseEpisodesReturn {
  const [episodes, setEpisodes] = useState<EpisodeSummary[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/episodes");
      if (!res.ok) {
        throw new Error(`Failed to fetch episodes (${res.status})`);
      }
      const data = await res.json();
      setEpisodes(data.episodes ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch episodes");
      setEpisodes([]);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { episodes, isLoading, error, refresh };
}
