import { useState } from "react";
import type { UseClusterReturn } from "@/hooks/use-cluster";
import { cn } from "@/lib/utils";
import type { ClusterSummary } from "./cluster-panel";

interface ClusterControlsProps {
  cluster: UseClusterReturn;
  summary?: ClusterSummary;
}

type BusyKind = "start" | "stop" | "collect" | null;

interface PerHostResult {
  host_id: string;
  status: "ok" | "error";
  state?: string;
  error?: string;
}

/**
 * Leader-only action cluster: Start All / Stop All / Collect Data.
 *
 * After each action, renders a per-host status strip so the operator
 * can see WHICH hosts accepted the trigger vs which errored out — this
 * is critical when a cross-network mDNS flake leaves a follower stuck
 * in a pre-recording state while the leader records happily.
 */
export function ClusterControls({ cluster, summary }: ClusterControlsProps) {
  const [busy, setBusy] = useState<BusyKind>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [messageTone, setMessageTone] = useState<"ok" | "err">("ok");
  const [lastResults, setLastResults] = useState<PerHostResult[] | null>(null);

  function show(msg: string, tone: "ok" | "err" = "ok") {
    setMessage(msg);
    setMessageTone(tone);
  }

  async function handleStart() {
    setBusy("start");
    setMessage(null);
    setLastResults(null);
    const res = await cluster.startAll();
    if (!res) {
      show("Start request failed — see leader logs", "err");
    } else {
      setLastResults(res.hosts);
      const ok = res.hosts.filter((h) => h.status === "ok").length;
      const total = res.hosts.length;
      if (total === 0) {
        // No followers, leader-only session — treat as success.
        show("Leader started (no followers)", "ok");
      } else {
        show(
          `${ok}/${total} follower${total === 1 ? "" : "s"} started`,
          ok === total ? "ok" : "err",
        );
      }
      cluster.refreshNow();
    }
    setBusy(null);
  }

  async function handleStop() {
    setBusy("stop");
    setMessage(null);
    setLastResults(null);
    const res = await cluster.stopAll();
    if (!res) {
      show("Stop request failed — see leader logs", "err");
    } else {
      setLastResults(res.hosts);
      const ok = res.hosts.filter((h) => h.status === "ok").length;
      const total = res.hosts.length;
      if (total === 0) {
        show("Leader stopped (no followers)", "ok");
      } else {
        show(
          `${ok}/${total} follower${total === 1 ? "" : "s"} stopped`,
          ok === total ? "ok" : "err",
        );
      }
      cluster.refreshNow();
    }
    setBusy(null);
  }

  async function handleCollect() {
    setBusy("collect");
    setMessage(null);
    setLastResults(null);
    const res = await cluster.collect();
    if (!res) {
      show("Collect request failed (still recording?)", "err");
    } else {
      const fileCount = res.hosts.reduce((n, h) => n + h.files.length, 0);
      show(`${fileCount} files from ${res.hosts.length} hosts`);
    }
    setBusy(null);
  }

  const btn =
    "rounded-lg border px-3 py-1 text-xs font-medium transition-colors " +
    "hover:bg-foreground/5 disabled:cursor-not-allowed disabled:opacity-50";

  const anyRecording = (summary?.recording ?? 0) > 0;
  const anyStopped = (summary?.stopped ?? 0) > 0;

  // Derive button emphasis so the obvious next action is visually primary.
  const startCls = cn(
    btn,
    !anyRecording && "border-recording/40 text-recording hover:bg-recording/5",
  );
  const stopCls = cn(
    btn,
    anyRecording && "border-destructive/40 text-destructive hover:bg-destructive/5",
  );
  const collectCls = cn(
    btn,
    anyStopped && !anyRecording && "border-primary/40 text-primary hover:bg-primary/5",
  );

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5">
        <button
          className={startCls}
          onClick={handleStart}
          disabled={busy !== null || anyRecording}
          title={
            anyRecording
              ? "A host is already recording — Stop All before starting a new session"
              : "Start recording on the leader AND fan out /session/start to every follower"
          }
        >
          {busy === "start" ? "Starting…" : "Start All"}
        </button>
        <button
          className={stopCls}
          onClick={handleStop}
          disabled={busy !== null || !anyRecording}
          title={
            anyRecording
              ? "Stop recording on the leader AND every follower"
              : "No host is recording"
          }
        >
          {busy === "stop" ? "Stopping…" : "Stop All"}
        </button>
        <button
          className={collectCls}
          onClick={handleCollect}
          disabled={busy !== null || anyRecording}
          title={
            anyRecording
              ? "Can't collect while recording — Stop All first"
              : "Pull every follower's recorded files onto the leader"
          }
        >
          {busy === "collect" ? "Collecting…" : "Collect Data"}
        </button>
      </div>

      {message && (
        <div
          className={cn(
            "text-[11px]",
            messageTone === "err" ? "text-destructive" : "text-muted",
          )}
        >
          {message}
        </div>
      )}

      {/* Per-host result breakdown (only shown when at least one host was addressed) */}
      {lastResults && lastResults.length > 0 && (
        <ul className="flex flex-col gap-0.5 rounded-md border bg-foreground/[0.02] px-2 py-1">
          {lastResults.map((r) => (
            <li
              key={r.host_id}
              className="flex items-center gap-1.5 text-[10px]"
            >
              <span
                className={cn(
                  "inline-block h-1.5 w-1.5 rounded-full",
                  r.status === "ok" ? "bg-success" : "bg-destructive",
                )}
              />
              <span className="font-mono text-foreground">{r.host_id}</span>
              {r.state && (
                <span className="text-muted">→ {r.state}</span>
              )}
              {r.error && (
                <span className="min-w-0 flex-1 truncate text-destructive">
                  {r.error}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
