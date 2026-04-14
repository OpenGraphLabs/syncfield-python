import { useState } from "react";
import type { UseClusterReturn } from "@/hooks/use-cluster";
import { cn } from "@/lib/utils";
import type { ClusterSummary } from "./cluster-panel";

interface ClusterControlsProps {
  cluster: UseClusterReturn;
  summary?: ClusterSummary;
}

/**
 * Leader-only cluster action: Collect Data.
 *
 * Start/Stop the cluster is done from the header's Record/Stop buttons —
 * ``session.start()`` on the leader flips the mDNS advert and unblocks
 * every follower via HTTP-polled health, so there's no separate
 * cluster-only trigger. Collect Data is the only action that needs a
 * cluster-scoped button: it's a leader-only post-stop operation that
 * pulls every follower's files onto the leader's disk.
 */
export function ClusterControls({ cluster, summary }: ClusterControlsProps) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [messageTone, setMessageTone] = useState<"ok" | "err">("ok");

  function show(msg: string, tone: "ok" | "err" = "ok") {
    setMessage(msg);
    setMessageTone(tone);
  }

  async function handleCollect() {
    setBusy(true);
    setMessage(null);
    const res = await cluster.collect();
    if (!res) {
      show("Collect request failed (still recording?)", "err");
    } else {
      const fileCount = res.hosts.reduce((n, h) => n + h.files.length, 0);
      if (fileCount === 0) {
        show("No files collected — followers may not have recorded yet", "err");
      } else {
        show(`Pulled ${fileCount} files from ${res.hosts.length} host${res.hosts.length === 1 ? "" : "s"}`);
      }
    }
    setBusy(false);
  }

  const anyRecording = (summary?.recording ?? 0) > 0;
  const anyStopped = (summary?.stopped ?? 0) > 0;

  const btn =
    "rounded-lg border px-3 py-1 text-xs font-medium transition-colors " +
    "hover:bg-foreground/5 disabled:cursor-not-allowed disabled:opacity-50";
  const collectCls = cn(
    btn,
    anyStopped && !anyRecording && "border-primary/40 text-primary hover:bg-primary/5",
  );

  return (
    <div className="flex flex-col gap-1.5">
      <button
        className={cn(collectCls, "self-start")}
        onClick={handleCollect}
        disabled={busy || anyRecording}
        title={
          anyRecording
            ? "Can't collect while any host is still recording — press Stop first"
            : "Pull every follower's recorded files onto the leader's disk"
        }
      >
        {busy ? "Collecting…" : "Collect Data"}
      </button>

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
