import type { SensorEvent } from "./types";

// Multiplexed envelope: same shape as SensorEvent with an added
// stream_id tag so the client can dispatch events to per-stream
// subscribers. Backed by /stream/sensors server-side.
interface MultiplexedSensorEvent extends SensorEvent {
  stream_id: string;
}

type Listener = (event: SensorEvent) => void;
type StatusListener = (connected: boolean) => void;

const RECONNECT_DELAY_MS = 2000;

/**
 * Shared SSE subscription for all sensor streams.
 *
 * React components (IMU cards, sensor charts, …) each render one
 * stream, but a per-component EventSource runs into the browser's
 * HTTP/1.1 6-connection-per-origin cap once a session carries more
 * than ~4 streams — later EventSources stay queued and never fire
 * onopen. This module multiplexes every mounted stream onto a single
 * connection: subscribers register by ``streamId`` and receive only
 * events tagged for them; the last unsubscribe closes the SSE.
 */
class SensorStore {
  private es: EventSource | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private listeners = new Map<string, Set<Listener>>();
  private statusListeners = new Set<StatusListener>();
  private connected = false;

  subscribe(streamId: string, cb: Listener): () => void {
    let set = this.listeners.get(streamId);
    if (!set) {
      set = new Set();
      this.listeners.set(streamId, set);
    }
    set.add(cb);
    this.ensureOpen();
    return () => {
      const current = this.listeners.get(streamId);
      if (!current) return;
      current.delete(cb);
      if (current.size === 0) this.listeners.delete(streamId);
      this.maybeClose();
    };
  }

  subscribeStatus(cb: StatusListener): () => void {
    this.statusListeners.add(cb);
    cb(this.connected);
    this.ensureOpen();
    return () => {
      this.statusListeners.delete(cb);
      this.maybeClose();
    };
  }

  private ensureOpen() {
    if (this.es || this.reconnectTimer) return;
    this.open();
  }

  private open() {
    const es = new EventSource("/stream/sensors");
    this.es = es;

    es.onopen = () => {
      this.setConnected(true);
    };

    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as MultiplexedSensorEvent;
        const { stream_id, ...rest } = data;
        const listeners = this.listeners.get(stream_id);
        if (!listeners) return;
        for (const cb of listeners) cb(rest as SensorEvent);
      } catch {
        // ignore malformed
      }
    };

    es.onerror = () => {
      this.setConnected(false);
      es.close();
      if (this.es === es) this.es = null;
      if (this.listeners.size === 0 && this.statusListeners.size === 0) return;
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        this.open();
      }, RECONNECT_DELAY_MS);
    };
  }

  private maybeClose() {
    if (this.listeners.size > 0 || this.statusListeners.size > 0) return;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.es) {
      this.es.close();
      this.es = null;
    }
    this.setConnected(false);
  }

  private setConnected(next: boolean) {
    if (this.connected === next) return;
    this.connected = next;
    for (const cb of this.statusListeners) cb(next);
  }
}

export const sensorStore = new SensorStore();
