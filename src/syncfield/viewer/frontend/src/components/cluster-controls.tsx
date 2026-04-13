import { useState } from "react";
import type { UseClusterReturn } from "@/hooks/use-cluster";
import { cn } from "@/lib/utils";

interface ClusterControlsProps {
  cluster: UseClusterReturn;
}

type BusyKind = "start" | "stop" | "collect" | null;

/**
 * Leader-only action cluster: Start All / Stop All / Collect Data.
 *
 * Each button disables while its request is in flight and reports the
 * per-host summary inline beneath the row.
 */
export function ClusterControls({ cluster }: ClusterControlsProps) {
  const [busy, setBusy] = useState<BusyKind>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [messageTone, setMessageTone] = useState<"ok" | "err">("ok");

  function show(msg: string, tone: "ok" | "err" = "ok") {
    setMessage(msg);
    setMessageTone(tone);
  }

  async function handleStart() {
    setBusy("start");
    setMessage(null);
    const res = await cluster.startAll();
    if (!res) {
      show("Start request failed", "err");
    } else {
      const ok = res.hosts.filter((h) => h.status === "ok").length;
      const total = res.hosts.length;
      show(`${ok}/${total} hosts started`, ok === total ? "ok" : "err");
      cluster.refreshNow();
    }
    setBusy(null);
  }

  async function handleStop() {
    setBusy("stop");
    setMessage(null);
    const res = await cluster.stopAll();
    if (!res) {
      show("Stop request failed", "err");
    } else {
      const ok = res.hosts.filter((h) => h.status === "ok").length;
      const total = res.hosts.length;
      show(`${ok}/${total} hosts stopped`, ok === total ? "ok" : "err");
      cluster.refreshNow();
    }
    setBusy(null);
  }

  async function handleCollect() {
    setBusy("collect");
    setMessage(null);
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

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5">
        <button
          className={btn}
          onClick={handleStart}
          disabled={busy !== null}
          title="Start recording on every reachable host"
        >
          {busy === "start" ? "Starting…" : "Start All"}
        </button>
        <button
          className={btn}
          onClick={handleStop}
          disabled={busy !== null}
          title="Stop recording on every reachable host"
        >
          {busy === "stop" ? "Stopping…" : "Stop All"}
        </button>
        <button
          className={btn}
          onClick={handleCollect}
          disabled={busy !== null}
          title="Pull the latest session files from every follower to the leader"
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
    </div>
  );
}
