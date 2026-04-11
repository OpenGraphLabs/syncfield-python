// ---------------------------------------------------------------------------
// Review types — mirrors the Python episode / sync API models
// ---------------------------------------------------------------------------

// Episode list item (from GET /api/episodes)
export interface EpisodeSummary {
  id: string;
  path: string;
  has_manifest: boolean;
  has_sync: boolean;
  stream_count: number;
  host_id: string | null;
  created_at: string; // ISO datetime
}

// Episode detail (from GET /api/episodes/{id})
export interface EpisodeDetail {
  id: string;
  manifest: EpisodeManifest | null;
  sync_report: SyncReport | null;
  has_synced_videos: boolean;
  streams: string[];
}

// Manifest structure (from manifest.json written by orchestrator)
export interface EpisodeManifest {
  host_id: string;
  sdk_version: string;
  streams: Record<string, ManifestStream>;
}

export interface ManifestStream {
  kind: string;
  capabilities: {
    provides_audio_track: boolean;
    produces_file: boolean;
  };
  status: string;
  frame_count: number;
  first_sample_at_ns: number | null;
  last_sample_at_ns: number | null;
}

// Sync report (from sync_report.json)
export interface SyncReport {
  summary: {
    status: string;
    synced_duration_sec: number;
    total_synced_frames: number;
    primary_stream: string;
    primary_host: string;
    max_drift_ms: number;
    actual_mean_fps: number;
  };
  streams: Record<string, SyncStreamResult>;
}

export interface SyncStreamResult {
  role: string;
  host: string;
  fps: number;
  original_duration_sec: number;
  original_frame_count: number;
  confidence?: number;
  offset_ms?: number;
  overlap_sec?: number;
}

// Sync quality grade derived from confidence
export type SyncGrade = "excellent" | "good" | "fair" | "poor" | "primary";

// Frame map entry (from frame_map.jsonl)
export interface FrameMapEntry {
  frame: number;
  original_frame: number;
  primary_time_sec: number;
  streams: Record<string, { frame: number; delta_ms: number }>;
}

// Sync job status (from polling)
export interface SyncJobStatus {
  job_id: string;
  status: "processing" | "complete" | "failed";
  progress: number;
  phase: string;
  result?: SyncReport;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Derive a human-readable grade from a stream's sync confidence. */
export function syncGrade(stream: SyncStreamResult): SyncGrade {
  if (stream.role === "primary") return "primary";
  const c = stream.confidence ?? 0;
  if (c >= 0.8) return "excellent";
  if (c >= 0.6) return "good";
  if (c >= 0.4) return "fair";
  return "poor";
}
