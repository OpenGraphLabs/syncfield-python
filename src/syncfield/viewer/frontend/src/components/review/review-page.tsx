import { useCallback, useEffect, useState } from "react";
import { EpisodeList } from "./episode-list";
import { EpisodeDetail } from "./episode-detail";

/**
 * Review mode — browse episodes and analyze sync quality.
 *
 * Routes:
 * - `/review`           → Episode list (grid or table)
 * - `/review/{ep_id}`   → Episode detail (video + sync analysis)
 *
 * Uses URL path for navigation so browser back/forward and
 * direct links work.
 */
export function ReviewPage() {
  const [episodeId, setEpisodeId] = useState<string | null>(
    getEpisodeIdFromUrl,
  );

  // Sync with browser back/forward
  useEffect(() => {
    const onPop = () => setEpisodeId(getEpisodeIdFromUrl());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const handleSelect = useCallback((id: string) => {
    window.history.pushState(null, "", `/review/${id}`);
    setEpisodeId(id);
  }, []);

  const handleBack = useCallback(() => {
    window.history.pushState(null, "", "/review");
    setEpisodeId(null);
  }, []);

  if (episodeId) {
    return <EpisodeDetail episodeId={episodeId} onBack={handleBack} />;
  }

  return <EpisodeList onSelect={handleSelect} />;
}

function getEpisodeIdFromUrl(): string | null {
  const path = window.location.pathname;
  const match = path.match(/^\/review\/(.+)$/);
  return match ? match[1]! : null;
}
