import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ClusterPeersResponse,
  ClusterHealthResponse,
  ClusterDevicesResponse,
  ClusterActionResponse,
  ClusterCollectResponse,
  ClusterConfigResponse,
} from "@/lib/types";

export interface UseClusterReturn {
  available: boolean;
  peers: ClusterPeersResponse | null;
  health: ClusterHealthResponse | null;
  config: ClusterConfigResponse | null;
  isLeader: boolean;
  startAll: () => Promise<ClusterActionResponse | null>;
  stopAll: () => Promise<ClusterActionResponse | null>;
  collect: () => Promise<ClusterCollectResponse | null>;
  discoverDevices: (kinds?: string[]) => Promise<ClusterDevicesResponse | null>;
  refreshNow: () => void;
}

const PEERS_INTERVAL_MS = 3000;
const HEALTH_INTERVAL_MS = 1000;

/**
 * Polls the cluster REST endpoints and exposes leader-only imperative actions.
 *
 * Single-host sessions return 409 from every cluster endpoint — we flip
 * `available` to `false` so UI that depends on the cluster can hide itself.
 */
export function useCluster(): UseClusterReturn {
  const [available, setAvailable] = useState(true);
  const [peers, setPeers] = useState<ClusterPeersResponse | null>(null);
  const [health, setHealth] = useState<ClusterHealthResponse | null>(null);
  const [config, setConfig] = useState<ClusterConfigResponse | null>(null);
  const mountedRef = useRef(true);
  const [refreshTrigger, setRefreshTrigger] = useState(0);

  // Single generic fetcher. Treats 409 as "cluster mode unavailable".
  const fetchJson = useCallback(async <T,>(url: string, init?: RequestInit): Promise<T | null> => {
    try {
      const r = await fetch(url, init);
      if (r.status === 409) {
        setAvailable(false);
        return null;
      }
      if (!r.ok) {
        return null;
      }
      setAvailable(true);
      return (await r.json()) as T;
    } catch {
      return null;
    }
  }, []);

  // Poll /peers + /config every 3s. Skip polling after cluster is known
  // unavailable (single-host mode returns 409 on every cluster endpoint —
  // continuing to poll floods the DevTools console with error logs).
  useEffect(() => {
    if (!available) return;
    let cancelled = false;
    async function tick() {
      if (cancelled) return;
      const [p, c] = await Promise.all([
        fetchJson<ClusterPeersResponse>("/api/cluster/peers"),
        fetchJson<ClusterConfigResponse>("/api/cluster/config"),
      ]);
      if (!cancelled) {
        setPeers(p);
        setConfig(c);
      }
    }
    tick();
    const timer = setInterval(tick, PEERS_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [fetchJson, refreshTrigger, available]);

  // Poll /health every 1s — same single-host skip.
  useEffect(() => {
    if (!available) return;
    let cancelled = false;
    async function tick() {
      if (cancelled) return;
      const h = await fetchJson<ClusterHealthResponse>("/api/cluster/health");
      if (!cancelled) setHealth(h);
    }
    tick();
    const timer = setInterval(tick, HEALTH_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [fetchJson, refreshTrigger, available]);

  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  const startAll = useCallback(
    () => fetchJson<ClusterActionResponse>("/api/cluster/start", { method: "POST" }),
    [fetchJson],
  );
  const stopAll = useCallback(
    () => fetchJson<ClusterActionResponse>("/api/cluster/stop", { method: "POST" }),
    [fetchJson],
  );
  const collect = useCallback(
    () => fetchJson<ClusterCollectResponse>("/api/cluster/collect", { method: "POST" }),
    [fetchJson],
  );
  const discoverDevices = useCallback(
    (kinds?: string[]) =>
      fetchJson<ClusterDevicesResponse>("/api/cluster/devices/discover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kinds, timeout: 10.0 }),
      }),
    [fetchJson],
  );

  const refreshNow = useCallback(() => setRefreshTrigger((n) => n + 1), []);

  const isLeader = peers?.self_role === "leader";

  return {
    available,
    peers,
    health,
    config,
    isLeader: !!isLeader,
    startAll,
    stopAll,
    collect,
    discoverDevices,
    refreshNow,
  };
}
