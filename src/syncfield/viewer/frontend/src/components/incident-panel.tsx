import { useState } from "react";
import type { IncidentSnapshot, Severity } from "../lib/types";
import { cn } from "@/lib/utils";

const SEVERITY_DOT: Record<Severity, string> = {
  info: "bg-muted",
  warning: "bg-warning",
  error: "bg-destructive",
  critical: "bg-destructive",
};

const SEVERITY_LABEL: Record<Severity, string> = {
  info: "Info",
  warning: "Warning",
  error: "Error",
  critical: "Critical",
};

function formatAgo(s: number): string {
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function IncidentCard({ inc, isOpen }: { inc: IncidentSnapshot; isOpen: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const hasDetails = !!inc.detail || inc.artifacts.length > 0;

  return (
    <div className="mb-1.5 overflow-hidden rounded-md border bg-card">
      <button
        type="button"
        onClick={() => hasDetails && setExpanded((v) => !v)}
        disabled={!hasDetails}
        aria-expanded={hasDetails ? expanded : undefined}
        className={cn(
          "flex w-full items-start gap-2 px-2.5 py-2 text-left transition-colors",
          hasDetails && "hover:bg-foreground/5",
          !hasDetails && "cursor-default",
        )}
        title={hasDetails ? (expanded ? "Hide details" : "Show details") : SEVERITY_LABEL[inc.severity]}
      >
        <span
          className={cn(
            "mt-1.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full",
            SEVERITY_DOT[inc.severity],
          )}
          aria-label={SEVERITY_LABEL[inc.severity]}
        />
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs text-foreground">
            <span className="font-mono text-muted">{inc.stream_id}</span>
            <span className="text-muted"> · </span>
            {inc.title}
          </div>
          <div className="mt-0.5 text-[11px] text-muted">
            {isOpen ? "opened " : "recovered "}
            {formatAgo(inc.ago_s)}
            {inc.event_count > 1 && ` · ${inc.event_count} events`}
            {inc.artifacts.length > 0 &&
              ` · ${inc.artifacts.length} artifact${inc.artifacts.length === 1 ? "" : "s"}`}
          </div>
        </div>
        {hasDetails && (
          <svg
            width="10"
            height="10"
            viewBox="0 0 10 10"
            fill="none"
            className={cn(
              "mt-1.5 shrink-0 text-muted transition-transform",
              expanded && "rotate-180",
            )}
            aria-hidden
          >
            <path
              d="M2.5 3.75L5 6.25L7.5 3.75"
              stroke="currentColor"
              strokeWidth="1.25"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        )}
      </button>

      {expanded && hasDetails && (
        <div className="space-y-1 border-t bg-background-subtle px-2.5 py-2">
          {inc.detail && (
            <pre className="select-text whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-muted">
              {inc.detail}
            </pre>
          )}
          {inc.artifacts.map((a) => (
            <div key={a.path} className="select-text break-all font-mono text-[11px] text-muted">
              <span className="text-foreground/60">{a.kind}:</span> {a.path}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function IncidentPanel({
  active,
  resolved,
}: {
  active: IncidentSnapshot[];
  resolved: IncidentSnapshot[];
}) {
  return (
    <section className="rounded-md border bg-background-subtle p-2.5">
      <SectionHeader label="Active issues" count={active.length} tone={active.length > 0 ? "warning" : "muted"} />
      {active.length === 0 ? (
        <p className="mb-3 px-0.5 text-[11px] text-muted">All clear.</p>
      ) : (
        <div className="mb-3">
          {active.map((inc) => (
            <IncidentCard key={inc.id} inc={inc} isOpen />
          ))}
        </div>
      )}

      <SectionHeader label="Resolved this session" count={resolved.length} tone="muted" />
      {resolved.length === 0 ? (
        <p className="px-0.5 text-[11px] text-muted">None.</p>
      ) : (
        resolved.map((inc) => <IncidentCard key={inc.id} inc={inc} isOpen={false} />)
      )}
    </section>
  );
}

function SectionHeader({
  label,
  count,
  tone,
}: {
  label: string;
  count: number;
  tone: "muted" | "warning";
}) {
  return (
    <header className="mb-1.5 flex items-center gap-1.5 px-0.5">
      <span className="text-[10px] font-medium uppercase tracking-wide text-muted">
        {label}
      </span>
      <span
        className={cn(
          "rounded-full px-1.5 py-0 text-[10px] font-semibold tabular-nums",
          tone === "warning"
            ? "bg-warning/15 text-foreground"
            : "bg-foreground/5 text-muted",
        )}
      >
        {count}
      </span>
    </header>
  );
}
