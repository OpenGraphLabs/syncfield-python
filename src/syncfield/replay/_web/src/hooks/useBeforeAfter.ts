import { useCallback, useEffect, useState } from "react";
import type { SyncReport } from "../types";

export type SyncMode = "before" | "after";

/** Pure helper — keeps the math testable in isolation. */
export function computeStreamTime(
  masterTime: number,
  offsetSeconds: number | undefined,
  mode: SyncMode,
): number {
  if (mode === "before") return masterTime;
  const offset = offsetSeconds ?? 0;
  return Math.max(0, masterTime - offset);
}

export interface BeforeAfterState {
  mode: SyncMode;
  toggle: () => void;
  setMode: (next: SyncMode) => void;
  offsetFor: (streamId: string) => number | undefined;
  hasReport: boolean;
}

export function useBeforeAfter(report: SyncReport | null): BeforeAfterState {
  const hasReport = report !== null;
  const [mode, setMode] = useState<SyncMode>(hasReport ? "after" : "before");

  // If a report shows up after initial render (slow fetch), default to after.
  useEffect(() => {
    if (hasReport) setMode("after");
  }, [hasReport]);

  const toggle = useCallback(() => {
    if (!hasReport) return;
    setMode((m) => (m === "after" ? "before" : "after"));
  }, [hasReport]);

  const offsetFor = useCallback(
    (streamId: string): number | undefined =>
      report?.streams[streamId]?.offset_seconds,
    [report],
  );

  // Keyboard shortcut: B
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      if (e.code === "KeyB") {
        e.preventDefault();
        toggle();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [toggle]);

  return { mode, toggle, setMode, offsetFor, hasReport };
}
