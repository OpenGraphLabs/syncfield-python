import { useEffect, useState } from "react";
import type { DiscoveredDevice } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Returns true when the BLE device name matches the Insta360 Go 3S pattern.
 * Matches names like "Insta360 Go 3S", "Go 3S *", or "go3s_*".
 */
function isGo3SDevice(name: string | undefined): boolean {
  if (!name) return false;
  const lower = name.toLowerCase();
  return lower.includes("go 3") || lower.includes("go3");
}

/**
 * Human-readable device type label derived from the discovered device's
 * adapter / name. Falls back to the raw adapter string for unknown types.
 */
function deviceTypeLabel(device: DiscoveredDevice): string {
  if (isGo3SDevice(device.name)) return "Insta360 Go3S";
  return device.adapter;
}

interface DiscoveryModalProps {
  isOpen: boolean;
  onClose: () => void;
  devices: DiscoveredDevice[];
  isScanning: boolean;
  error: string | null;
  onScan: () => void;
  onAdd: (deviceId: string) => Promise<boolean>;
}

/**
 * Device discovery modal — scan for devices, select, and add to session.
 */
export function DiscoveryModal({
  isOpen,
  onClose,
  devices,
  isScanning,
  error,
  onScan,
  onAdd,
}: DiscoveryModalProps) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState(false);

  // Auto-scan on open
  useEffect(() => {
    if (isOpen) {
      onScan();
      setSelected(new Set());
    }
  }, [isOpen, onScan]);

  if (!isOpen) return null;

  function toggleDevice(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function handleAdd() {
    setAdding(true);
    for (const id of selected) {
      await onAdd(id);
    }
    setAdding(false);
    setSelected(new Set());
    onClose();
  }

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-foreground/40 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* Dialog */}
      <div className="relative w-full max-w-md rounded-2xl border bg-card p-6 shadow-xl">
        {/* Header */}
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-semibold">Discover Devices</h2>
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
              Scanning…
            </>
          ) : (
            <>
              <span className="inline-block h-2 w-2 rounded-full bg-success" />
              {devices.length} device{devices.length !== 1 ? "s" : ""} found
            </>
          )}
        </div>

        {/* Error */}
        {error && (
          <div className="mb-3 rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive">
            {error}
          </div>
        )}

        {/* Device list */}
        <div className="max-h-64 overflow-y-auto">
          {devices.length === 0 && !isScanning ? (
            <p className="py-8 text-center text-xs text-muted">
              No devices found
            </p>
          ) : (
            <ul className="space-y-1">
              {devices.map((device) => (
                <li key={device.id}>
                  <label
                    className={cn(
                      "flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2 transition-colors",
                      selected.has(device.id)
                        ? "bg-primary/10"
                        : "hover:bg-foreground/5",
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(device.id)}
                      onChange={() => toggleDevice(device.id)}
                      className="accent-primary"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-xs font-medium">
                        {device.name}
                      </div>
                      <div className="text-[10px] text-muted">
                        {deviceTypeLabel(device)} · {device.kind}
                      </div>
                    </div>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Actions */}
        <div className="mt-4 flex items-center justify-end gap-2">
          <button
            onClick={onScan}
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
          <button
            onClick={handleAdd}
            disabled={selected.size === 0 || adding}
            className={cn(
              "rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors",
              "hover:bg-primary/90 disabled:opacity-40",
            )}
          >
            {adding
              ? "Adding…"
              : `Add ${selected.size > 0 ? selected.size : ""} →`}
          </button>
        </div>
      </div>
    </div>
  );
}
