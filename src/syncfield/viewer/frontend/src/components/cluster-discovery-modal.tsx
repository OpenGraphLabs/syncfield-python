import { useEffect, useState } from "react";
import type {
  ClusterDevicesResponse,
  ClusterHostDevices,
} from "@/lib/types";
import type { UseClusterReturn } from "@/hooks/use-cluster";
import { cn } from "@/lib/utils";

interface ClusterDiscoveryModalProps {
  isOpen: boolean;
  onClose: () => void;
  cluster: UseClusterReturn;
}

/**
 * Cluster-wide device discovery modal. On open, fires a single
 * `POST /api/cluster/devices/discover` and renders the per-host result.
 * Read-only — adding devices is still a per-host concern driven from each
 * host's own viewer, so this screen exists to give the leader visibility
 * across the fleet.
 */
export function ClusterDiscoveryModal({
  isOpen,
  onClose,
  cluster,
}: ClusterDiscoveryModalProps) {
  const [response, setResponse] = useState<ClusterDevicesResponse | null>(null);
  const [isScanning, setIsScanning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function rescan() {
    setIsScanning(true);
    setError(null);
    try {
      const res = await cluster.discoverDevices();
      if (!res) {
        setError("Discovery failed (is this the leader?)");
        setResponse(null);
      } else {
        setResponse(res);
      }
    } finally {
      setIsScanning(false);
    }
  }

  useEffect(() => {
    if (isOpen) {
      rescan();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  if (!isOpen) return null;

  const hosts = response?.hosts ?? [];
  const totalDevices = hosts.reduce((n, h) => n + h.devices.length, 0);

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-foreground/40 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* Dialog */}
      <div className="relative w-full max-w-2xl rounded-2xl border bg-card p-6 shadow-xl">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-semibold">Discover Across Cluster</h2>
          <button
            onClick={onClose}
            className="rounded-lg p-1 text-muted transition-colors hover:bg-foreground/5"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path
                d="M4 4L12 12M12 4L4 12"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
              />
            </svg>
          </button>
        </div>

        {/* Scan status */}
        <div className="mb-3 flex items-center gap-2 text-xs text-muted">
          {isScanning ? (
            <>
              <span className="inline-block h-2 w-2 animate-spin rounded-full border border-primary border-t-transparent" />
              Scanning cluster…
            </>
          ) : (
            <>
              <span className="inline-block h-2 w-2 rounded-full bg-success" />
              {totalDevices} device{totalDevices !== 1 ? "s" : ""} across{" "}
              {hosts.length} host{hosts.length !== 1 ? "s" : ""}
            </>
          )}
        </div>

        {error && (
          <div className="mb-3 rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive">
            {error}
          </div>
        )}

        {/* Per-host groups */}
        <div className="max-h-[50vh] space-y-3 overflow-y-auto">
          {hosts.map((host) => (
            <HostDevicesBlock key={host.host_id} host={host} />
          ))}
          {hosts.length === 0 && !isScanning && !error && (
            <p className="py-8 text-center text-xs text-muted">
              No hosts responded
            </p>
          )}
        </div>

        {/* Actions */}
        <div className="mt-4 flex items-center justify-end gap-2">
          <button
            onClick={rescan}
            disabled={isScanning}
            className={cn(
              "rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors",
              "hover:bg-foreground/5 disabled:opacity-40",
            )}
          >
            Rescan
          </button>
          <button
            onClick={onClose}
            className="rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors hover:bg-foreground/5"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

function HostDevicesBlock({ host }: { host: ClusterHostDevices }) {
  const hasError = host.status === "error" || host.error;
  return (
    <section className="rounded-lg border">
      <header className="flex items-center gap-2 border-b bg-background-subtle px-3 py-1.5">
        <span
          className={cn(
            "inline-block h-1.5 w-1.5 rounded-full",
            hasError ? "bg-destructive" : "bg-success",
          )}
        />
        <span className="font-mono text-[11px]">
          {host.host_id}
          {host.is_self && <span className="ml-1 text-muted">(self)</span>}
        </span>
        <div className="flex-1" />
        <span className="text-[10px] text-muted">
          {host.devices.length} device{host.devices.length !== 1 ? "s" : ""} ·{" "}
          {host.duration_s.toFixed(1)}s
        </span>
      </header>

      {hasError && (
        <div className="px-3 py-2 text-[11px] text-destructive">
          {host.error ?? "Discovery failed on this host"}
        </div>
      )}

      {host.devices.length > 0 && (
        <ul className="divide-y divide-border/50">
          {host.devices.map((d) => (
            <li
              key={d.device_id}
              className="flex items-center gap-2 px-3 py-1.5"
            >
              <div className="min-w-0 flex-1">
                <div className="truncate text-xs font-medium">
                  {d.display_name}
                </div>
                <div className="text-[10px] text-muted">
                  {d.adapter_type} · {d.kind}
                  {d.in_use && (
                    <span className="ml-2 text-warning">in use</span>
                  )}
                </div>
              </div>
              <span className="shrink-0 font-mono text-[10px] text-muted">
                {d.device_id}
              </span>
            </li>
          ))}
        </ul>
      )}

      {Object.keys(host.errors).length > 0 && (
        <div className="border-t px-3 py-1.5 text-[10px] text-muted">
          Adapter errors:{" "}
          {Object.entries(host.errors)
            .map(([k, v]) => `${k}: ${v}`)
            .join("; ")}
        </div>
      )}

      {host.timed_out.length > 0 && (
        <div className="border-t px-3 py-1.5 text-[10px] text-warning">
          Timed out: {host.timed_out.join(", ")}
        </div>
      )}
    </section>
  );
}
