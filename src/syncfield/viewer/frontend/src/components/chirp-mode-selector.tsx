import { useEffect, useRef, useState } from "react";
import type { ChirpMode } from "@/lib/types";

interface ChirpModeSelectorProps {
  /** Current chirp mode reported by the snapshot. */
  value: ChirpMode;
  /**
   * Whether the selector should reject input. Set this when the session
   * is mid-lifecycle (CONNECTING / PREPARING / COUNTDOWN / RECORDING /
   * STOPPING) — the SDK rejects with HTTP 409 in those states anyway,
   * but graying out is the friendly hint.
   */
  disabled?: boolean;
}

const MODES: { value: ChirpMode; title: string; help: string }[] = [
  {
    value: "ultrasound",
    title: "Ultrasonic",
    help: "17–19 kHz, inaudible to most adults",
  },
  {
    value: "audible",
    title: "Audible",
    help: "400–2500 Hz, human-audible",
  },
  {
    value: "off",
    title: "Off",
    help: "no start/stop chirp; 3/2/1 countdown still plays",
  },
];

/**
 * Compact 3-way segmented selector for the orchestrator chirp mode.
 *
 * Posts to ``/api/chirp-mode`` on click. Uses optimistic local state
 * so the click is immediately reflected visually; the snapshot
 * eventually catches up via WebSocket and replaces the local
 * override. If the POST fails, the local override is reverted and an
 * error is shown.
 *
 * Resilience considerations from production debugging:
 *
 * - **No pending lock across buttons.** A previous design gated every
 *   button on a global `pending` flag during the in-flight fetch.
 *   In rare cases (snapshot push race, fetch promise stuck), the
 *   gate stayed asserted forever and subsequent clicks were dropped.
 *   This implementation uses optimistic UI instead: each click is
 *   independent and the UI updates instantly.
 * - **Same-mode re-clicks are tolerated** so a user can re-trigger
 *   the POST if the snapshot disagrees with reality.
 * - **Console diagnostics.** Each click logs the request/response
 *   path under the ``chirp-mode`` namespace so users can inspect
 *   dev tools when behavior surprises them.
 */
export function ChirpModeSelector({
  value,
  disabled = false,
}: ChirpModeSelectorProps) {
  // Optimistic override — what the user *just* picked. Cleared when the
  // snapshot catches up so the snapshot becomes authoritative again.
  const [localOverride, setLocalOverride] = useState<ChirpMode | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const aliveRef = useRef(true);

  // Snapshot caught up to our pick → drop the override.
  useEffect(() => {
    if (localOverride !== null && localOverride === value) {
      setLocalOverride(null);
    }
  }, [value, localOverride]);

  // Stale error becomes irrelevant when the user re-enters a usable state.
  useEffect(() => {
    if (!disabled) setErrorMsg(null);
  }, [disabled]);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const effective = localOverride ?? value;

  const submitMode = async (
    next: ChirpMode,
  ): Promise<{ ok: true } | { ok: false; status: number; detail: string }> => {
    const resp = await fetch("/api/chirp-mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: next }),
    });
    let body: { error?: string; mode?: string } | null = null;
    try {
      body = (await resp.json()) as { error?: string; mode?: string };
    } catch {
      /* server may return empty body on some errors */
    }
    if (resp.ok) {
      console.info("[chirp-mode] server accepted", body);
      return { ok: true };
    }
    const detail = body?.error ?? `HTTP ${resp.status}`;
    return { ok: false, status: resp.status, detail };
  };

  const handleClick = async (next: ChirpMode) => {
    if (disabled) {
      console.warn(
        "[chirp-mode] click ignored — selector disabled (session likely mid-lifecycle)",
      );
      return;
    }
    console.info("[chirp-mode] requesting mode change", {
      from: value,
      override: localOverride,
      to: next,
    });
    setLocalOverride(next);
    setErrorMsg(null);

    // The SDK rejects with 409 if the session is mid-lifecycle. After
    // a Disconnect click, there's a brief window where the orchestrator
    // is still holding its lock to tear down adapters — our request
    // can race ahead and land while state is still ``connected``. Retry
    // a few times across ~1.2s to absorb that race transparently before
    // surfacing the error to the user.
    const RETRY_DELAYS_MS = [300, 500, 800];
    let attempt = 0;
    while (true) {
      try {
        const result = await submitMode(next);
        if (result.ok) return; // success — leave override; snapshot will catch up

        if (result.status === 409 && attempt < RETRY_DELAYS_MS.length) {
          const delay = RETRY_DELAYS_MS[attempt]!;
          console.warn(
            `[chirp-mode] 409 (state transition); retrying in ${delay}ms`,
            { attempt: attempt + 1, detail: result.detail },
          );
          await new Promise((r) => window.setTimeout(r, delay));
          attempt += 1;
          if (!aliveRef.current) return;
          continue;
        }

        console.error("[chirp-mode] server rejected", {
          status: result.status,
          detail: result.detail,
          attempts: attempt + 1,
        });
        if (aliveRef.current) {
          // Replace the raw RuntimeError text with a UX-friendly hint
          // for the common 409 case. The selector is gated to
          // idle/stopped via the parent's `disabled` prop, so 409 here
          // is almost always a brief race — disconnect just initiated,
          // mode change landed before the state transition completed.
          const message =
            result.status === 409
              ? "Disconnect first to change chirp mode."
              : result.detail;
          setErrorMsg(message);
          setLocalOverride(null);
        }
        return;
      } catch (err) {
        const detail = err instanceof Error ? err.message : String(err);
        console.error("[chirp-mode] network error", detail);
        if (aliveRef.current) {
          setErrorMsg(detail);
          setLocalOverride(null);
        }
        return;
      }
    }
  };

  return (
    <div className="flex items-center gap-2">
      <div
        role="radiogroup"
        aria-label="Chirp mode"
        title={
          disabled
            ? "Chirp mode is locked while connected — Disconnect first to change."
            : undefined
        }
        className={`inline-grid grid-cols-3 rounded-md border bg-background-subtle p-0.5 ${
          disabled ? "opacity-50 saturate-0" : ""
        }`}
      >
        {MODES.map(({ value: mode, title, help }) => {
          const checked = mode === effective;
          const isOverridden = localOverride !== null && mode === localOverride;
          const baseTone = checked
            ? disabled
              ? "bg-muted/70 text-card"
              : "bg-foreground text-card shadow-sm"
            : disabled
              ? "text-muted/70"
              : "text-muted hover:bg-card hover:text-foreground";
          return (
            <button
              key={mode}
              type="button"
              role="radio"
              aria-checked={checked}
              aria-label={title}
              title={
                disabled
                  ? "Disconnect to change chirp mode"
                  : help
              }
              disabled={disabled}
              onClick={() => handleClick(mode)}
              className={`flex h-6 min-w-[68px] items-center justify-center rounded px-2 text-[10.5px] font-medium transition-colors ${baseTone} ${
                disabled ? "cursor-not-allowed" : "cursor-pointer"
              } ${isOverridden ? "ring-1 ring-foreground/40" : ""}`}
            >
              {title}
            </button>
          );
        })}
      </div>
      {disabled && (
        <span
          className="text-[10.5px] text-muted"
          title="Chirp mode can only be changed in idle or stopped state"
        >
          (disconnect to change)
        </span>
      )}
      {errorMsg && (
        <span
          role="alert"
          title={errorMsg}
          className="max-w-[300px] truncate rounded bg-destructive/10 px-1.5 py-0.5 text-[10.5px] text-destructive"
        >
          {errorMsg}
        </span>
      )}
    </div>
  );
}
