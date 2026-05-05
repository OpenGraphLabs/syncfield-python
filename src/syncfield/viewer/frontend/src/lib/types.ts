// ---------------------------------------------------------------------------
// Snapshot types — mirrors the Python SessionSnapshot / StreamSnapshot
// ---------------------------------------------------------------------------

export interface StreamCapabilities {
  provides_audio_track: boolean;
  supports_precise_timestamps: boolean;
  is_removable: boolean;
  produces_file: boolean;
  /** False for standalone-recorder streams (e.g. Insta360 Go3S) that have no live MJPEG feed. */
  live_preview: boolean;
}

export interface StreamSnapshot {
  id: string;
  kind: "video" | "audio" | "sensor" | "custom";
  frame_count: number;
  effective_hz: number;
  last_sample_ms_ago: number | null;
  provides_audio_track: boolean;
  produces_file: boolean;
  /** Stream capabilities declared by the adapter. May be absent on older servers. */
  capabilities?: StreamCapabilities;
  connection_state: ConnectionState;
  connection_error: string | null;
}

export type ChirpMode = "ultrasound" | "audible" | "off";

export interface ChirpInfo {
  enabled: boolean;
  /** Named preset the current SyncToneConfig falls into. */
  mode: ChirpMode;
  start_ns: number | null;
  stop_ns: number | null;
}

export type Severity = "info" | "warning" | "error" | "critical";

export type ConnectionState =
  | "idle"
  | "connecting"
  | "connected"
  | "failed"
  | "disconnected";

export interface IncidentArtifact {
  kind: string;
  path: string;
  detail: string | null;
}

export interface IncidentSnapshot {
  id: string;
  stream_id: string;
  fingerprint: string;
  title: string;
  severity: Severity;
  source: string;
  opened_at_ns: number;
  closed_at_ns: number | null;
  event_count: number;
  detail: string | null;
  ago_s: number;
  artifacts: IncidentArtifact[];
}

// ---------------------------------------------------------------------------
// Aggregation types (Insta360 Go3S)
// ---------------------------------------------------------------------------

export type AggregationState = "pending" | "running" | "completed" | "failed";

export interface AggregationActiveJob {
  job_id: string;
  episode_id: string;
  state: AggregationState;
  cameras_total: number;
  cameras_done: number;
  current_stream_id: string | null;
  current_bytes: number;
  current_total_bytes: number;
  /** Current pipeline phase — useful when the bar would otherwise sit at
   *  0% during a lengthy WiFi switch. One of "switching_wifi" | "probing" |
   *  "downloading" | "restoring_wifi" | "starting" | null. */
  stage: string | null;
  error: string | null;
}

export interface AggregationSnapshotWS {
  active_job: AggregationActiveJob | null;
  queue_length: number;
  recent_jobs: AggregationActiveJob[];
}

export interface SessionSnapshot {
  type: "snapshot";
  state: SessionState;
  host_id: string;
  elapsed_s: number;
  chirp: ChirpInfo;
  streams: Record<string, StreamSnapshot>;
  active_incidents: IncidentSnapshot[];
  resolved_incidents: IncidentSnapshot[];
  output_dir: string;
  /** Aggregation state for Go3S streams; present when a Go3S adapter is active. */
  aggregation?: AggregationSnapshotWS;
}

export type SessionState =
  | "idle"
  | "connecting"
  | "connected"
  | "starting"
  | "recording"
  | "stopping"
  | "stopped"
  | "disconnecting";

export interface CountdownEvent {
  type: "countdown";
  count: number;
}

export interface StopResultEvent {
  type: "stop_result";
  status: "saving" | "success" | "partial" | "error";
  cancelled?: boolean;
  output_dir?: string;
  manifest_ok?: boolean;
  sync_point_ok?: boolean;
  error?: string;
  streams?: Record<
    string,
    {
      status: string;
      frame_count: number;
      file_exists?: boolean;
      has_output?: boolean;
      error?: string;
      warning?: string;
    }
  >;
}

export type ServerMessage = SessionSnapshot | CountdownEvent | StopResultEvent;

// ---------------------------------------------------------------------------
// Control commands (client → server)
// ---------------------------------------------------------------------------

export type ControlAction =
  | "connect"
  | "disconnect"
  | "record"
  | "stop"
  | "cancel"
  | "retry_aggregation"
  | "cancel_aggregation"
  | "aggregate_episode"
  | "aggregate_all_pending";

export interface ControlCommand {
  action: ControlAction;
  countdown_s?: number;
}

// ---------------------------------------------------------------------------
// Discovery types
// ---------------------------------------------------------------------------

export interface DiscoveredDevice {
  id: string;
  name: string;
  adapter: string;
  kind: string;
  description: string;
  in_use: boolean;
  warnings: string[];
}

// ---------------------------------------------------------------------------
// Sensor SSE data
// ---------------------------------------------------------------------------

export interface SensorEvent {
  channels: Record<string, number>;
  // List / vector-valued channels (e.g. MetaQuestHandStream's
  // ``hand_joints``: 156 floats). Present when the adapter emits
  // non-scalar samples. ``null`` when the stream is scalar-only.
  pose: Record<string, number[]> | null;
  label: number | null;
}

export interface Quest3Frame {
  hand_joints?: number[];      // 156 floats (26 × 3 × 2 hands)
  joint_rotations?: number[];  // 208 floats (26 × 4 × 2 hands)
  head_pose?: number[];        // 7 floats (pos3 + quat4 xyzw)
  mode?: "hands" | "controller";
}

// ---------------------------------------------------------------------------
// Type guards
// ---------------------------------------------------------------------------

export function isSnapshot(msg: ServerMessage): msg is SessionSnapshot {
  return msg.type === "snapshot";
}

export function isCountdown(msg: ServerMessage): msg is CountdownEvent {
  return msg.type === "countdown";
}

export function isStopResult(msg: ServerMessage): msg is StopResultEvent {
  return msg.type === "stop_result";
}

// ---------------------------------------------------------------------------
// Multi-host cluster types
// ---------------------------------------------------------------------------

export interface ClusterPeer {
  host_id: string;
  role: string;          // "leader" | "follower"
  status: string;        // mDNS advert status: "preparing" | "recording" | "stopped"
  sdk_version: string;
  chirp_enabled: boolean;
  control_plane_port: number | null;
  resolved_address: string | null;
  is_self: boolean;
  reachable: boolean | null;
}

export interface ClusterPeersResponse {
  session_id: string;
  self_host_id: string;
  self_role: string;
  peers: ClusterPeer[];
}

export interface ClusterStreamHealth {
  id: string;
  kind: string;
  fps: number;
  frames: number;
  dropped: number;
  last_frame_ns: number | null;
  bytes_written: number;
}

export interface ClusterHostHealth {
  host_id: string;
  is_self: boolean;
  status: "ok" | "unreachable" | "error";
  rtt_ms: number | null;
  error?: string;
  health?: {
    host_id: string;
    role: string;
    state: string;
    sdk_version: string;
    uptime_s: number;
  };
  streams?: ClusterStreamHealth[];
}

export interface ClusterHealthResponse {
  session_id: string;
  hosts: ClusterHostHealth[];
}

export interface ClusterDevice {
  adapter_type: string;
  kind: string;
  display_name: string;
  description: string;
  device_id: string;
  in_use: boolean;
  warnings: string[];
  accepts_output_dir: boolean;
}

export interface ClusterHostDevices {
  host_id: string;
  is_self: boolean;
  status: "ok" | "error";
  devices: ClusterDevice[];
  errors: Record<string, string>;
  timed_out: string[];
  duration_s: number;
  error?: string;
}

export interface ClusterDevicesResponse {
  hosts: ClusterHostDevices[];
}

export interface ClusterActionResult {
  host_id: string;
  status: "ok" | "error";
  state?: string;
  error?: string;
}

export interface ClusterActionResponse {
  hosts: ClusterActionResult[];
}

export interface ClusterCollectResponse {
  session_id: string;
  leader_host_id: string;
  hosts: {
    host_id: string;
    status: string;
    files: Array<{ path: string; size: number; sha256: string; mtime_ns: number }>;
    error: string | null;
  }[];
}

export interface ClusterConfigResponse {
  session_id: string;
  applied_config: {
    session_name: string;
    start_chirp: { from_hz: number; to_hz: number; duration_ms: number; amplitude: number; envelope_ms: number };
    stop_chirp: { from_hz: number; to_hz: number; duration_ms: number; amplitude: number; envelope_ms: number };
    recording_mode: string;
  } | null;
}
