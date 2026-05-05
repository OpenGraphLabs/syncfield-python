import type { DashboardLayout } from "./stream-dashboard-layout";

export const STREAM_PANEL_DASHBOARD_LAYOUT_STORAGE_KEY =
  "syncfield.streamPanelDashboard.layout.v1";

interface DashboardStorageOptions {
  storageKey?: string;
  fallbackStorageKeys?: string[];
  storage?: Storage;
}

export function readStoredDashboardLayout({
  storageKey = STREAM_PANEL_DASHBOARD_LAYOUT_STORAGE_KEY,
  fallbackStorageKeys = [],
  storage = defaultStorage(),
}: DashboardStorageOptions = {}): DashboardLayout | null {
  if (!storage) return null;
  for (const key of [storageKey, ...fallbackStorageKeys]) {
    const layout = parseStoredDashboardLayout(storage.getItem(key));
    if (layout) return layout;
  }
  return null;
}

export function writeStoredDashboardLayout(
  layout: DashboardLayout,
  {
    storageKey = STREAM_PANEL_DASHBOARD_LAYOUT_STORAGE_KEY,
    storage = defaultStorage(),
  }: Pick<DashboardStorageOptions, "storageKey" | "storage"> = {},
) {
  if (!storage) return;
  storage.setItem(storageKey, JSON.stringify(layout));
}

function parseStoredDashboardLayout(raw: string | null): DashboardLayout | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as DashboardLayout;
    if (!parsed || typeof parsed !== "object") return null;
    return parsed;
  } catch {
    return null;
  }
}

function defaultStorage(): Storage | undefined {
  if (typeof window === "undefined") return undefined;
  return window.localStorage;
}
