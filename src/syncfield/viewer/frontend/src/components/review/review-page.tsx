import { useCallback, useEffect, useState } from "react";
import { EpisodeList } from "./episode-list";
import { EpisodeDetail } from "./episode-detail";
import { CollectVideosBar } from "./collect-videos-bar";
import { useSession } from "@/hooks/use-session";

/**
 * Review mode — browse episodes and analyze sync quality.
 *
 * Routes:
 * - `/review`           → Episode list (grid or table) + Collect Videos action
 * - `/review/{ep_id}`   → Episode detail (video + sync analysis)
 *
 * Uses URL path for navigation so browser back/forward and
 * direct links work.
 */
export function ReviewPage() {
  const { snapshot, sendCommand } = useSession();
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

  // Show the Collect Videos action only if the session has at least one
  // standalone-recorder stream (Go3S today). For sessions without one
  // there's nothing to collect from a USB camera.
  const hasGo3sStream = Object.values(snapshot?.streams ?? {}).some(
    (s) => s.kind === "video" && s.capabilities?.live_preview === false,
  );

  return (
    <div className="flex h-full flex-col">
      {hasGo3sStream && (
        <CollectVideosBar
          onCollect={() => sendCommand("aggregate_all_pending")}
        />
      )}
      <div className="flex-1 overflow-auto">
        <EpisodeList
          onSelect={handleSelect}
          aggregation={snapshot?.aggregation}
        />
      </div>
    </div>
  );
}

function getEpisodeIdFromUrl(): string | null {
  const path = window.location.pathname;
  const match = path.match(/^\/review\/(.+)$/);
  return match ? match[1]! : null;
}
