import { useCallback, useState } from "react";
import type { DiscoveredDevice } from "@/lib/types";

interface UseDiscoveryReturn {
  /** List of discovered devices from the most recent scan. */
  devices: DiscoveredDevice[];
  /** Whether a scan is currently in progress. */
  isScanning: boolean;
  /** Error message from the last failed scan, if any. */
  error: string | null;
  /** Trigger a new device scan. */
  scan: () => Promise<void>;
  /** Add a discovered device to the session by ID. */
  addDevice: (deviceId: string) => Promise<boolean>;
  /** Remove a stream from the session by ID. */
  removeStream: (streamId: string) => Promise<boolean>;
}

/**
 * REST hook for device discovery and stream management.
 *
 * Provides `scan()` to trigger `/api/discover`, `addDevice()` to
 * POST to `/api/streams/{id}`, and `removeStream()` to DELETE.
 */
export function useDiscovery(): UseDiscoveryReturn {
  const [devices, setDevices] = useState<DiscoveredDevice[]>([]);
  const [isScanning, setIsScanning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const scan = useCallback(async () => {
    setIsScanning(true);
    setError(null);
    try {
      const res = await fetch("/api/discover", { method: "POST" });
      const data = await res.json();
      if (data.error) {
        setError(data.error);
      }
      setDevices(data.devices ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Scan failed");
      setDevices([]);
    } finally {
      setIsScanning(false);
    }
  }, []);

  const addDevice = useCallback(async (deviceId: string): Promise<boolean> => {
    try {
      const res = await fetch(`/api/streams/${deviceId}`, { method: "POST" });
      return res.ok;
    } catch {
      return false;
    }
  }, []);

  const removeStream = useCallback(
    async (streamId: string): Promise<boolean> => {
      try {
        const res = await fetch(`/api/streams/${streamId}`, {
          method: "DELETE",
        });
        return res.ok;
      } catch {
        return false;
      }
    },
    [],
  );

  return { devices, isScanning, error, scan, addDevice, removeStream };
}
