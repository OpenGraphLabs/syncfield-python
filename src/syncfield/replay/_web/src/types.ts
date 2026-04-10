export type StreamKind = "video" | "sensor" | "custom";

export interface ReplayStream {
  id: string;
  kind: StreamKind;
  media_url: string | null;
  data_url: string | null;
  frame_count: number;
}

export interface SyncPoint {
  monotonic_ns?: number;
  wall_clock_ns?: number;
  iso_datetime?: string;
  chirp_start_ns?: number;
  chirp_stop_ns?: number;
}

export interface SessionManifest {
  host_id: string;
  sync_point: SyncPoint;
  has_frame_map: boolean;
  streams: ReplayStream[];
}

export type SyncQuality = "excellent" | "good" | "fair" | "poor";

export interface SyncStreamResult {
  offset_seconds: number;
  confidence: number;
  quality: SyncQuality;
}

export interface SyncReport {
  streams: Record<string, SyncStreamResult>;
}
