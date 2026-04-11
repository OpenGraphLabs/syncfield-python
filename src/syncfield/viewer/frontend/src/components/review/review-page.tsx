import { useState } from "react";
import { EpisodeList } from "./episode-list";
import { EpisodeDetail } from "./episode-detail";

/**
 * Review mode — browse episodes and analyze sync quality.
 *
 * Two-level navigation:
 * 1. Episode list (grid or table)
 * 2. Episode detail (video + sync analysis)
 */
export function ReviewPage() {
  const [selectedEpisode, setSelectedEpisode] = useState<string | null>(null);

  if (selectedEpisode) {
    return (
      <EpisodeDetail
        episodeId={selectedEpisode}
        onBack={() => setSelectedEpisode(null)}
      />
    );
  }

  return <EpisodeList onSelect={setSelectedEpisode} />;
}
