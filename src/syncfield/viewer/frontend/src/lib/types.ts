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
}

export interface ChirpInfo {
  enabled: boolean;
  start_ns: number | null;
  stop_ns: number | null;
}

export interface HealthEntry {
  stream_id: string;
  kind: string;
  at_s: number;
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
