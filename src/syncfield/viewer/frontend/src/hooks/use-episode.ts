import { useCallback, useEffect, useState } from "react";
import type { EpisodeDetail } from "@/lib/review-types";

interface UseEpisodeReturn {
  /** Episode detail, or null while loading / on error. */
  episode: EpisodeDetail | null;
  /** Whether the episode detail is being loaded. */
  isLoading: boolean;
  /** Error message from the last failed fetch, if any. */
  error: string | null;
  /** Re-fetch the episode detail. */
  refresh: () => Promise<void>;
}

/**
 * REST hook for a single episode's detail.
 *
 * Fetches `GET /api/episodes/{id}` on mount and whenever `episodeId`
 * changes. Exposes a `refresh()` callback for manual re-fetching.
 */
export function useEpisode(episodeId: string | null): UseEpisodeReturn {
  const [episode, setEpisode] = useState<EpisodeDetail | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!episodeId) {
      setEpisode(null);
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/episodes/${episodeId}`);
      if (!res.ok) {
        throw new Error(`Failed to fetch episode (${res.status})`);
      }
      const data: EpisodeDetail = await res.json();
      setEpisode(data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to fetch episode",
      );
      setEpisode(null);
    } finally {
      setIsLoading(false);
    }
  }, [episodeId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { episode, isLoading, error, refresh };
}
