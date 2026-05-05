import { Clock3, X } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import type {
  AggregationSnapshotWS,
  IncidentSnapshot,
  Severity,
  StreamSnapshot,
} from "@/lib/types";
import { formatCount, formatHz } from "@/lib/format";
import { cn } from "@/lib/utils";
import { AudioLevelChart } from "./audio-level-chart";
import { SensorPanel } from "./sensor-panel";
import {
  StandaloneRecorderPanel,
  type StandaloneRecorderStream,
} from "./standalone-recorder-panel";
import { Spinner } from "./spinner";
import {
  StreamPanelDashboard,
  type StreamPanelDashboardItem,
} from "./stream-panel-dashboard";
import { ConnectingOverlay, FailedOverlay } from "./stream-overlays";
import { VideoPreview } from "./video-preview";

// ---------------------------------------------------------------------------
// Public surface
// ---------------------------------------------------------------------------

interface StreamDashboardProps {
  streams: StreamSnapshot[];
  canRemove: boolean;
  onRemove: (streamId: string) => void;
  /** Active incidents from the session snapshot — used to derive per-stream severity badge. */
  activeIncidents?: IncidentSnapshot[];
  /** Session state string — forwarded to StandaloneRecorderPanel for recording detection. */
  sessionState?: string;
  /** Top-level aggregation snapshot from the WS payload — used by StandaloneRecorderPanel. */
  aggregation?: AggregationSnapshotWS;
  /** Callback to send an aggregation retry command for a given job ID. */
  onRetryAggregation?: (jobId: string) => void;
}

/**
 * Grafana-style draggable / resizable stream panel grid.
 *
 * Each :class:`StreamSnapshot` becomes a :type:`StreamPanelDashboardItem`
 * (header / badges / actions / body / footer). The layout is persisted in
 * ``localStorage`` per :module:`stream-panel-dashboard-storage`. Reset
 * via the icon button in the dashboard header.
 *
 * Behavior parity with the previous flex-grid card layout:
 *
 * - Standalone recorders (e.g. Insta360 Go3S) get the
 *   :component:`StandaloneRecorderPanel` body with its own footer stats.
 * - Sensor streams get the 2/5/15/60s window length control in the header.
 * - The disconnecting overlay dims each panel body during teardown.
 * - Incident counts are summarised by per-stream severity badge.
 */
export function StreamDashboard({
  streams,
  canRemove,
  onRemove,
  activeIncidents = [],
  sessionState,
  aggregation,
  onRetryAggregation,
}: StreamDashboardProps) {
  const [sensorWindows, setSensorWindows] = useState<
    Record<string, SensorWindowSeconds>
  >(readStoredSensorWindows);
  const isRecording = sessionState === "recording";

  useEffect(() => {
    writeStoredSensorWindows(sensorWindows);
  }, [sensorWindows]);

  const updateSensorWindow = (
    streamId: string,
    seconds: SensorWindowSeconds,
  ) => {
    setSensorWindows((current) => ({ ...current, [streamId]: seconds }));
  };

  const items = useMemo<StreamPanelDashboardItem[]>(
    () =>
      streams.map((stream) => buildDashboardItem({
        stream,
        canRemove,
        onRemove,
        activeIncidents,
        sessionState,
        isRecording,
        aggregation,
        onRetryAggregation,
        sensorWindow:
          sensorWindows[stream.id] ?? DEFAULT_SENSOR_WINDOW_SECONDS,
        onSensorWindowChange: (seconds) => updateSensorWindow(stream.id, seconds),
      })),
    [
      streams,
      canRemove,
      onRemove,
      activeIncidents,
      sessionState,
      isRecording,
      aggregation,
      onRetryAggregation,
      sensorWindows,
    ],
  );

  return (
    <StreamPanelDashboard
      items={items}
      title="Streams"
      subtitle={`${streams.length} ${streams.length === 1 ? "panel" : "panels"}${sessionState ? ` · ${sessionState}` : ""}`}
      className="mb-5"
      resetTitle="Reset layout"
    />
  );
}

// ---------------------------------------------------------------------------
// Per-stream item composition
// ---------------------------------------------------------------------------

interface BuildItemArgs {
  stream: StreamSnapshot;
  canRemove: boolean;
  onRemove: (streamId: string) => void;
  activeIncidents: IncidentSnapshot[];
  sessionState?: string;
  isRecording: boolean;
  aggregation?: AggregationSnapshotWS;
  onRetryAggregation?: (jobId: string) => void;
  sensorWindow: SensorWindowSeconds;
  onSensorWindowChange: (seconds: SensorWindowSeconds) => void;
}

function buildDashboardItem({
  stream,
  canRemove,
  onRemove,
  activeIncidents,
  sessionState,
  isRecording,
  aggregation,
  onRetryAggregation,
  sensorWindow,
  onSensorWindowChange,
}: BuildItemArgs): StreamPanelDashboardItem {
  const incidentStats = streamIncidentStats(stream.id, activeIncidents);
  const isStandalone =
    stream.kind === "video" && stream.capabilities?.live_preview === false;

  // The dashboard header already renders {item.kind} as a chip — only
  // emit the *additional* tags here so we don't duplicate the kind label.
  const badges = (
    <>
      {isStandalone && <Tag>standalone</Tag>}
      {stream.provides_audio_track && stream.kind !== "audio" && (
        <Tag>audio</Tag>
      )}
      {stream.produces_file && <Tag>file</Tag>}
      {incidentStats.count > 0 && incidentStats.highest && (
        <IncidentBadge
          count={incidentStats.count}
          severity={incidentStats.highest}
        />
      )}
    </>
  );

  const headerActions = (
    <>
      {stream.kind === "sensor" && (
        <SensorWindowControl
          value={sensorWindow}
          onChange={onSensorWindowChange}
        />
      )}
      {canRemove && (
        <button
          type="button"
          data-no-drag
          onClick={() => onRemove(stream.id)}
          title={`Remove ${stream.id}`}
          className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted transition-colors hover:bg-foreground/5 hover:text-destructive"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </>
  );

  const body = isStandalone ? (
    <StandaloneBody
      stream={stream}
      sessionState={sessionState}
      aggregation={aggregation}
      onRetryAggregation={onRetryAggregation}
    />
  ) : (
    <LiveStreamBody stream={stream} sensorWindowSeconds={sensorWindow} />
  );

  const wrappedBody = (
    <div className="relative h-full w-full">
      {body}
      {sessionState === "disconnecting" && <DisconnectingOverlay />}
    </div>
  );

  return {
    id: stream.id,
    kind: stream.kind,
    title: stream.id,
    statusTone: streamStatusTone(stream),
    accentTone:
      isRecording && stream.connection_state === "connected"
        ? "recording"
        : undefined,
    badges,
    headerActions,
    body: wrappedBody,
    footer: <StreamFooter stream={stream} isStandalone={isStandalone} />,
  };
}

// ---------------------------------------------------------------------------
// Bodies
// ---------------------------------------------------------------------------

function LiveStreamBody({
  stream,
  sensorWindowSeconds,
}: {
  stream: StreamSnapshot;
  sensorWindowSeconds: SensorWindowSeconds;
}) {
  if (stream.connection_state === "connecting") {
    return <ConnectingOverlay variant="fill" />;
  }
  if (stream.connection_state === "failed") {
    return (
      <FailedOverlay
        error={stream.connection_error ?? "Unknown error"}
        variant="fill"
      />
    );
  }
  // Connected / idle / disconnected — adapter latest_frame is populated
  // on connect (not on record start), so previews can render immediately.
  if (stream.kind === "video") {
    return <VideoPreview streamId={stream.id} variant="fill" />;
  }
  if (stream.kind === "audio") {
    return <AudioLevelChart streamId={stream.id} variant="fill" />;
  }
  if (stream.kind === "sensor") {
    return (
      <SensorPanel
        streamId={stream.id}
        windowSeconds={sensorWindowSeconds}
        variant="fill"
      />
    );
  }
  return (
    <div className="flex h-full w-full items-center justify-center text-xs text-muted">
      No preview
    </div>
  );
}

function StandaloneBody({
  stream,
  sessionState,
  aggregation,
  onRetryAggregation,
}: {
  stream: StreamSnapshot;
  sessionState?: string;
  aggregation?: AggregationSnapshotWS;
  onRetryAggregation?: (jobId: string) => void;
}) {
  const standaloneStream: StandaloneRecorderStream = {
    id: stream.id,
    sessionState: sessionState ?? "idle",
    frame_count: stream.frame_count,
  };
  const activeJob = mapAggregationForStream(aggregation, stream.id);
  return (
    <div className="h-full w-full">
      <StandaloneRecorderPanel
        stream={standaloneStream}
        aggregation={activeJob}
        onRetry={
          activeJob?.job_id && onRetryAggregation
            ? () => onRetryAggregation(activeJob.job_id)
            : undefined
        }
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Footer
// ---------------------------------------------------------------------------

function StreamFooter({ stream }: { stream: StreamSnapshot; isStandalone: boolean }) {
  return (
    <>
      <span className="font-mono">{formatCount(stream.frame_count)}</span>
      <span className="h-3 w-px bg-border" />
      <span className="font-mono">{formatHz(stream.effective_hz)}</span>
      <span className="min-w-0 flex-1 truncate text-right">
        {stream.connection_state}
      </span>
    </>
  );
}

// ---------------------------------------------------------------------------
// Per-stream incident helpers
// ---------------------------------------------------------------------------

const SEVERITY_ORDER: Severity[] = ["info", "warning", "error", "critical"];
const BADGE_COLOR: Record<Severity, string> = {
  info: "bg-muted text-white",
  warning: "bg-warning text-foreground",
  error: "bg-destructive text-white",
  critical: "bg-destructive text-white",
};
const SEVERITY_LABEL: Record<Severity, string> = {
  info: "info",
  warning: "warning",
  error: "error",
  critical: "critical",
};

function IncidentBadge({ count, severity }: { count: number; severity: Severity }) {
  return (
    <span
      title={`${count} active ${SEVERITY_LABEL[severity]} ${count === 1 ? "issue" : "issues"} — see Active Issues panel`}
      aria-label={`${count} ${SEVERITY_LABEL[severity]} ${count === 1 ? "issue" : "issues"}`}
      className={cn(
        "inline-flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[10px] font-semibold leading-none tabular-nums",
        BADGE_COLOR[severity],
      )}
    >
      {count}
    </span>
  );
}

function streamIncidentStats(streamId: string, active: IncidentSnapshot[]) {
  const mine = active.filter((i) => i.stream_id === streamId);
  const count = mine.length;
  let highest: Severity | null = null;
  for (const i of mine) {
    if (
      highest === null ||
      SEVERITY_ORDER.indexOf(i.severity) > SEVERITY_ORDER.indexOf(highest)
    ) {
      highest = i.severity;
    }
  }
  return { count, highest };
}

// ---------------------------------------------------------------------------
// Sensor window control (per-stream visible time range)
// ---------------------------------------------------------------------------

const SENSOR_WINDOW_OPTIONS = [2, 5, 15, 60] as const;
const DEFAULT_SENSOR_WINDOW_SECONDS: SensorWindowSeconds = 5;
const SENSOR_WINDOW_STORAGE_KEY = "syncfield.viewer.sensorWindows.v1";

type SensorWindowSeconds = (typeof SENSOR_WINDOW_OPTIONS)[number];

function SensorWindowControl({
  value,
  onChange,
}: {
  value: SensorWindowSeconds;
  onChange: (seconds: SensorWindowSeconds) => void;
}) {
  return (
    <div
      data-no-drag
      className="inline-flex h-7 shrink-0 items-center gap-0.5 rounded-md border bg-background-subtle p-0.5 text-[10px] text-muted"
      title="Visible sensor window"
      aria-label="Visible sensor window"
    >
      <Clock3 className="mx-1 h-3 w-3 shrink-0" />
      <div className="flex min-w-0 items-center gap-0.5">
        {SENSOR_WINDOW_OPTIONS.map((seconds) => (
          <button
            key={seconds}
            type="button"
            data-no-drag
            onClick={() => onChange(seconds)}
            className={cn(
              "inline-flex h-5 min-w-6 items-center justify-center rounded px-1 font-mono transition-colors",
              value === seconds
                ? "bg-card text-foreground shadow-sm ring-1 ring-border"
                : "text-muted hover:bg-card/70 hover:text-foreground",
            )}
            aria-pressed={value === seconds}
          >
            {seconds}s
          </button>
        ))}
      </div>
    </div>
  );
}

function readStoredSensorWindows(): Record<string, SensorWindowSeconds> {
  if (typeof window === "undefined") return {};
  const raw = window.localStorage.getItem(SENSOR_WINDOW_STORAGE_KEY);
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw) as Record<string, number>;
    return Object.fromEntries(
      Object.entries(parsed).filter(
        (entry): entry is [string, SensorWindowSeconds] =>
          SENSOR_WINDOW_OPTIONS.includes(entry[1] as SensorWindowSeconds),
      ),
    );
  } catch {
    return {};
  }
}

function writeStoredSensorWindows(
  windows: Record<string, SensorWindowSeconds>,
) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(
    SENSOR_WINDOW_STORAGE_KEY,
    JSON.stringify(windows),
  );
}

// ---------------------------------------------------------------------------
// Status / placeholder helpers
// ---------------------------------------------------------------------------

function streamStatusTone(
  stream: StreamSnapshot,
): StreamPanelDashboardItem["statusTone"] {
  if (stream.connection_state === "failed") return "error";
  if (stream.connection_state === "connected" && stream.effective_hz > 0) {
    return "success";
  }
  return "muted";
}

function Tag({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-md bg-foreground/5 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted">
      {children}
    </span>
  );
}

/**
 * Faint dim + spinner overlaid on the panel body while the session is
 * tearing down — gives every panel immediate visual feedback that the
 * Disconnect action was received.
 */
function DisconnectingOverlay() {
  return (
    <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-background/55 backdrop-blur-[1px]">
      <div className="flex items-center gap-2 rounded-md border bg-card/95 px-2.5 py-1.5 text-xs text-foreground shadow-sm">
        <Spinner className="h-3 w-3 text-muted" />
        Disconnecting…
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Aggregation helpers
// ---------------------------------------------------------------------------

/**
 * Returns the active aggregation job if it involves the given stream, or the
 * most-recent job for that stream from recent_jobs. Falls back to the active
 * job unconditionally when stream_id is unavailable (older server).
 */
function mapAggregationForStream(
  agg: AggregationSnapshotWS | undefined,
  streamId: string,
) {
  if (!agg) return null;
  const job = agg.active_job;
  if (job) {
    if (!job.current_stream_id || job.current_stream_id === streamId) {
      return job;
    }
  }
  for (const rj of agg.recent_jobs) {
    if (!rj.current_stream_id || rj.current_stream_id === streamId) {
      return rj;
    }
  }
  return null;
}
