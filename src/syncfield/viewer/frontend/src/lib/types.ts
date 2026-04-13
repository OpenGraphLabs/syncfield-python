// ---------------------------------------------------------------------------
// Snapshot types — mirrors the Python SessionSnapshot / StreamSnapshot
// ---------------------------------------------------------------------------

export interface StreamSnapshot {
  id: string;
  kind: "video" | "audio" | "sensor" | "custom";
  frame_count: number;
  effective_hz: number;
  last_sample_ms_ago: number | null;
  provides_audio_track: boolean;
  produces_file: boolean;
  health_count: number;
  /** Count of non-heartbeat events (warnings/errors/drops). */
  problem_count: number;
}

export interface ChirpInfo {
  enabled: boolean;
  start_ns: number | null;
  stop_ns: number | null;
}

export interface HealthEntry {
  stream_id: string;
  kind: string;
  ago_s: number;
  detail: string | null;
}

export interface SessionSnapshot {
  type: "snapshot";
  state: SessionState;
  host_id: string;
  elapsed_s: number;
  chirp: ChirpInfo;
  streams: Record<string, StreamSnapshot>;
  health_log: HealthEntry[];
  output_dir: string;
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
  | "cancel";

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
  label: number | null;
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
