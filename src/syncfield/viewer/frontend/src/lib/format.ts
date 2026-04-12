/**
 * Display formatting helpers — ported from Python viewer/widgets/formatting.py.
 *
 * Single source of truth for how numbers, timestamps, and labels are
 * rendered across the entire web viewer.
 */

/** Format a duration as `MM:SS.mmm`. */
export function formatElapsed(seconds: number): string {
  if (seconds < 0) seconds = 0;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds - minutes * 60;
  const whole = Math.floor(remainder);
  let millis = Math.round((remainder - whole) * 1000);
  if (millis >= 1000) millis = 999;
  return `${pad2(minutes)}:${pad2(whole)}.${pad3(millis)}`;
}

/** Format a frequency for display as `29.9 Hz`. */
export function formatHz(hz: number): string {
  if (hz <= 0) return "—";
  if (hz >= 100) return `${Math.round(hz)} Hz`;
  return `${hz.toFixed(1)} Hz`;
}

/** Format a frame/sample count with thousands separators. */
export function formatCount(count: number): string {
  return count.toLocaleString("en-US");
}

/** Format `ms_ago` as a human-readable string. */
export function formatMsAgo(msAgo: number | null): string {
  if (msAgo === null) return "—";
  if (msAgo < 0) return "0 ms ago";
  if (msAgo < 1000) return `${Math.round(msAgo)} ms ago`;
  if (msAgo < 60_000) return `${(msAgo / 1000).toFixed(1)} s ago`;
  return `${(msAgo / 60_000).toFixed(1)} min ago`;
}

/** Truncate a long path from the left so the tail (episode id) stays visible. */
export function formatPathTail(path: string, maxChars = 60): string {
  if (path.length <= maxChars) return path;
  return "…" + path.slice(-(maxChars - 1));
}

/** Format chirp start/stop pair for the session clock panel. */
export function formatChirpPair(
  startNs: number | null,
  stopNs: number | null,
): string {
  if (startNs === null) return "pending";
  const startS = startNs / 1e9;
  if (stopNs === null) return `start @ ${startS.toFixed(3)}s`;
  const spanMs = (stopNs - startNs) / 1e6;
  return `start + ${Math.round(spanMs)} ms span`;
}

/** Uppercase a session state for the header chip. */
export function stateLabel(state: string): string {
  return state ? state.toUpperCase() : "";
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function pad2(n: number): string {
  return n < 10 ? `0${n}` : `${n}`;
}

function pad3(n: number): string {
  if (n < 10) return `00${n}`;
  if (n < 100) return `0${n}`;
  return `${n}`;
}
